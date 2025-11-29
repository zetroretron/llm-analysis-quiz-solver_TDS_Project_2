import os
import json
import asyncio
import logging
from playwright.async_api import async_playwright
from openai import AsyncOpenAI
from tools import execute_python_code
from dotenv import load_dotenv

# Load env vars
load_dotenv()

# Initialize logger
logger = logging.getLogger(__name__)

# Initialize OpenAI client
client = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))

async def get_page_content(page):
    """
    Extracts relevant content from the page.
    """
    # Wait for the content to load. The prompt mentions #result might be populated by JS.
    # We'll wait for network idle or a specific element if we knew it.
    # For now, wait for network idle.
    try:
        await page.wait_for_load_state("networkidle", timeout=5000)
    except:
        pass # Continue even if timeout, maybe content is there.

    # Extract text. We might want HTML to see structure, but text is cheaper.
    # However, the prompt says "human-readable JavaScript-rendered HTML page".
    # And "The quiz page always includes the submit URL to use."
    # We need to find the submit URL.
    
    content = await page.content()
    text = await page.evaluate("document.body.innerText")
    return content, text

async def solve_single_step(page, email: str, secret: str, current_url: str):
    """
    Solves a single step of the quiz.
    Returns the next URL or None if finished.
    """
    logger.info(f"Navigating to {current_url}")
    await page.goto(current_url)
    
    html_content, text_content = await get_page_content(page)
    
    # LLM Prompt to understand the task
    system_prompt = """
    You are an intelligent agent solving a data analysis quiz.
    You will be given the text content of a web page.
    Your goal is to:
    1. Identify the question/task.
    2. Identify the submission URL.
    3. Write Python code to solve the task if necessary (e.g. downloading files, calculating things).
    4. Provide the final JSON payload to submit.
    
    The submission payload must be:
    {
        "email": "...",
        "secret": "...",
        "url": "...", # The CURRENT quiz URL you are on
        "answer": ... # The answer you calculated
    }
    
    You have access to a python environment. 
    If you need to calculate something, output a JSON with "action": "code" and "code": "..."
    If you have the answer, output a JSON with "action": "submit" and "payload": {...}
    
    The 'email' and 'secret' are provided to you in the user prompt.
    """
    
    user_prompt = f"""
    Current URL: {current_url}
    Email: {email}
    Secret: {secret}
    
    Page Text:
    {text_content}
    
    (If the page text is insufficient, look at the HTML structure implied by the text or ask to see HTML - but for now try with text).
    
    What should I do?
    """
    
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt}
    ]
    
    # Simple loop to handle code execution
    for _ in range(3): # Max 3 turns (Code -> Result -> Submit)
        response = await client.chat.completions.create(
            model="gpt-4o-mini", # Or whatever model is available/preferred
            messages=messages,
            response_format={"type": "json_object"}
        )
        
        response_content = response.choices[0].message.content
        logger.info(f"LLM Response: {response_content}")
        
        try:
            action_data = json.loads(response_content)
        except json.JSONDecodeError:
            logger.error("Failed to parse JSON from LLM")
            break
            
        if action_data.get("action") == "code":
            code = action_data.get("code")
            logger.info(f"Executing code: {code}")
            result = execute_python_code(code)
            logger.info(f"Code result: {result}")
            
            messages.append({"role": "assistant", "content": response_content})
            messages.append({"role": "user", "content": f"Code Output:\n{result}\n\nNow proceed to submit."})
            
        elif action_data.get("action") == "submit":
            payload = action_data.get("payload")
            submit_url = action_data.get("submit_url") # LLM should extract this too, or we find it.
            
            # Wait, the prompt says "Post your answer to ... with this JSON payload".
            # The LLM should extract the submit URL.
            
            if not submit_url:
                # Fallback: try to find it in the text or assume a standard endpoint?
                # The prompt says "The quiz page always includes the submit URL to use."
                # Let's ask LLM to be sure to include it.
                logger.warning("No submit_url provided by LLM. Checking payload...")
                # Sometimes the payload has the url, but that's the QUIZ url.
                # We need the POST endpoint.
                pass

            # If LLM didn't give submit_url, we might be stuck. 
            # Let's assume the LLM does its job if prompted correctly.
            # We'll refine the system prompt in a bit if needed.
            
            logger.info(f"Submitting to {submit_url} with payload {payload}")
            
            # Use Playwright to submit? Or requests?
            # "Your script must ... submit ... to the specified endpoint".
            # Requests is easier.
            
            submit_response = requests.post(submit_url, json=payload)
            logger.info(f"Submission response: {submit_response.status_code} - {submit_response.text}")
            
            try:
                response_json = submit_response.json()
                if response_json.get("correct"):
                    logger.info("Answer correct!")
                    return response_json.get("url") # Next URL
                else:
                    logger.warning(f"Answer incorrect: {response_json.get('reason')}")
                    # Retry? The prompt says "you are allowed to re-submit".
                    # For now, let's just return None or handle retry logic.
                    # If we have a retry loop, we would continue.
                    # Let's just return None to stop for now, or maybe retry once?
                    return None
            except:
                logger.error("Failed to parse submission response")
                return None
                
        else:
            logger.warning("Unknown action")
            break
            
    return None

async def solve_quiz(email: str, secret: str, start_url: str):
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        
        current_url = start_url
        while current_url:
            next_url = await solve_single_step(page, email, secret, current_url)
            if next_url == current_url:
                logger.warning("Loop detected or same URL returned. Stopping.")
                break
            current_url = next_url
            
        await browser.close()
