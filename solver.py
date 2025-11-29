import os
import json
import asyncio
import logging
import re
from playwright.async_api import async_playwright
import google.generativeai as genai
from tools import execute_python_code
from dotenv import load_dotenv
from urllib.parse import urljoin

# Load env vars
load_dotenv()

# Initialize logger
logger = logging.getLogger(__name__)

# Configure Gemini
genai.configure(api_key=os.getenv("GEMINI_API_KEY"))

async def get_page_content(page):
    """
    Extracts relevant content from the page, including text and potential submission URLs.
    """
    try:
        await page.wait_for_load_state("networkidle", timeout=5000)
    except:
        pass 

    # Get simplified HTML
    content = await page.evaluate("""() => {
        const clone = document.body.cloneNode(true);
        const styles = clone.querySelectorAll('style');
        styles.forEach(s => s.remove());
        return clone.innerHTML;
    }""")
    
    # Fetch external scripts to find hidden URLs
    script_contents = await page.evaluate("""async () => {
        const scripts = Array.from(document.querySelectorAll('script[src]'));
        const contents = [];
        for (const s of scripts) {
            try {
                const response = await fetch(s.src);
                const text = await response.text();
                contents.push(`<!-- Script: ${s.src} -->\\n<script>\\n${text.slice(0, 5000)}\\n</script>`);
            } catch (e) {
                contents.push(`<!-- Failed to fetch ${s.src} -->`);
            }
        }
        return contents.join('\\n');
    }""")
    
    full_content = content + "\n\n" + script_contents
    return full_content

async def solve_single_step(page, email: str, secret: str, current_url: str):
    """
    Solves a single step of the quiz using Gemini.
    """
    logger.info(f"Navigating to {current_url}")
    await page.goto(current_url)
    
    html_content = await get_page_content(page)
    
    # Get page text separately for URL extraction
    page_text = await page.evaluate("document.body.innerText || document.body.textContent")
    
    # Debug logging
    logger.info(f"Page text (first 500 chars): {page_text[:500]}")
    
    # Extract submission URL using regex (more reliable than asking LLM)
    submission_url = None
    url_patterns = [
        r'(?:Post your (?:JSON )?answer to:|POST your JSON answer to:)\s*(https?://[^\s\'"<>]+)',
        r'(?:POST this JSON to)\s*(https?://[^\s\'"<>]+)',  # For "POST this JSON to" format
        r'(?:Submit to|POST to)\s*(https?://[^\s\'"<>]+)',
        r'POST the .+ back to\s+(/\w+)',  # Relative URL like "/submit"
    ]
    
    for pattern in url_patterns:
        match = re.search(pattern, page_text, re.IGNORECASE)
        if match:
            submission_url = match.group(1)
            # If it's a relative URL, make it absolute
            if submission_url.startswith('/'):
                submission_url = urljoin(current_url, submission_url)
            logger.info(f"✅ Found submission URL via regex: {submission_url}")
            break
    
    if not submission_url:
        logger.warning("⚠️ Could not find submission URL in page text")
    
    # Gemini Prompt - simplified since we extract URL programmatically
    system_prompt = """
    You are an intelligent agent solving a data analysis quiz.
    Your goal is to:
    1. Identify the question/task from the page content.
    2. Write Python code to solve the task if necessary.
    3. Provide the final answer.
    
    You have access to a python environment with pandas, requests, beautifulsoup4.
    
    RESPONSE FORMAT (Strict JSON):
    If you need to calculate something:
    {
        "action": "code",
        "code": "..."
    }
    
    If you have the answer and are ready to submit:
    {
        "action": "submit",
        "answer": ... # The answer (can be number, string, boolean, or JSON object)
    }
    
    IMPORTANT:
    - If your code produces an error, DO NOT submit the error message as the answer
    - Instead, debug and fix your code, then try again
    - Print intermediate results to help debug
    - Check column names and data structure before accessing them
    
    CRITICAL RULES:
    - Return ONLY valid JSON. No conversational text.
    - Use the email and secret provided to you in your code if needed
    """
    
    user_prompt = f"""
    Current URL: {current_url}
    Email: {email}
    Secret: {secret}
    
    PAGE TEXT:
    {page_text[:5000]}
    
    Page HTML (Simplified):
    {html_content[:2000]}
    
    What should I do?
    """
    
    model = genai.GenerativeModel('gemini-2.0-flash')
    
    # Simple loop to handle code execution
    for _ in range(3): # Max 3 turns
        try:
            chat = model.start_chat(history=[
                {"role": "user", "parts": [system_prompt]}
            ])
            
            response = await chat.send_message_async(user_prompt)
            response_content = response.text
            
            logger.info(f"LLM Response: {response_content}")

            # Robust JSON extraction
            json_match = re.search(r'\{.*\}', response_content, re.DOTALL)
            if json_match:
                json_str = json_match.group(0)
                action_data = json.loads(json_str)
            else:
                logger.error("No JSON found in response")
                continue
            
            if action_data.get("action") == "error":
                logger.error(f"LLM reported error: {action_data.get('message')}")
                break
                
            elif action_data.get("action") == "code":
                code = action_data.get("code")
                logger.info(f"Executing code: {code}")
                result = execute_python_code(code)
                logger.info(f"Code result: {result}")
                
                # Check if code execution had an error
                if "Execution Error:" in result or "Traceback" in result:
                    user_prompt = f"Code Output (ERROR):\n{result}\n\nYour code had an error. Please debug and fix it, then try again. Do NOT submit this error as the answer."
                else:
                    user_prompt = f"Code Output:\n{result}\n\nNow provide the final answer for submission."
                # Continue loop
                
            elif action_data.get("action") == "submit":
                answer = action_data.get("answer")
                
                # Use the URL we extracted earlier
                if not submission_url:
                    logger.error("No submission URL was found in the page text")
                    break
                
                # Build the payload
                payload = {
                    "email": email,
                    "secret": secret,
                    "url": current_url,
                    "answer": answer
                }
                
                logger.info(f"Submitting to {submission_url} with payload {payload}")
                
                # Submit
                import requests
                submit_response = requests.post(submission_url, json=payload)
                logger.info(f"Submission response: {submit_response.status_code} - {submit_response.text}")
                
                response_json = submit_response.json()
                if response_json.get("correct"):
                    logger.info("✅ Answer correct!")
                    return response_json.get("url")
                else:
                    logger.warning(f"❌ Answer incorrect: {response_json.get('reason')}")
                    return None
                    
            else:
                break
                
        except Exception as e:
            logger.error(f"Error in solver loop: {e}")
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
                break
            current_url = next_url
            
        await browser.close()
