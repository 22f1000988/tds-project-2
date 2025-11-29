import re
import json
import time
import base64
from typing import Dict, Any, Optional
from urllib.parse import urljoin

import httpx
import pandas as pd
import pdfplumber
from playwright.sync_api import sync_playwright
from bs4 import BeautifulSoup

def _maybe_decode_atob_text(text: str) -> Optional[str]:
    m = re.search(r"atob\(`([A-Za-z0-9+/=\n]+)`\)", text)
    if m:
        try:
            raw = m.group(1)
            decoded = base64.b64decode(raw).decode('utf-8', errors='replace')
            return decoded
        except Exception:
            return None
    m2 = re.search(r"([A-Za-z0-9+/=\n]{200,})", text)
    if m2:
        try:
            decoded = base64.b64decode(m2.group(1)).decode('utf-8', errors='replace')
            return decoded
        except Exception:
            return None
    return None

def solve_quiz_from_url(payload: Dict[str, Any], timeout_seconds: int = 160) -> Dict[str, Any]:
    email = payload.get('email')
    secret = payload.get('secret')
    url = payload.get('url')
    if not url:
        raise ValueError('No url provided')

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
        page = browser.new_page()
        page.goto(url, wait_until='networkidle', timeout=60_000)

        content = page.content()
        text = page.inner_text('body')

        decoded = _maybe_decode_atob_text(content) or _maybe_decode_atob_text(text)
        quiz_obj = None
        if decoded:
            try:
                jq = re.search(r"\{[\s\S]*\}", decoded)
                if jq:
                    quiz_obj = json.loads(jq.group(0))
            except Exception:
                quiz_obj = None

        if quiz_obj:
            result = _handle_quiz_object(quiz_obj, page, email, secret)
            browser.close()
            return result

        links = page.query_selector_all('a')
        file_links = []
        for a in links:
            href = a.get_attribute('href') or ''
            if href and any(ext in href.lower() for ext in ['.pdf', '.csv', '.xlsx']):
                file_links.append(urljoin(url, href))

        submit_url = None
        body = content
        m = re.search(r"Post your answer to\s*([\S]+)", body)
        if m:
            submit_url = m.group(1).strip()

        if file_links:
            pdfs = [l for l in file_links if l.lower().endswith('.pdf')]
            if pdfs:
                ans = _process_pdf_and_answer(pdfs[0])
                if submit_url:
                    submission = _submit_answer(submit_url, email, secret, ans)
                else:
                    submission = {"answer": ans}
                browser.close()
                return {"source": pdfs[0], "submitted": submission}

        forms = page.query_selector_all('form')
        for f in forms:
            act = f.get_attribute('action')
            if act:
                submit_url = urljoin(url, act)
                break

        browser.close()
        return {"status": "no-handled-case", "submit_url": submit_url}

def _handle_quiz_object(obj: Dict[str, Any], page, email: str, secret: str) -> Dict[str, Any]:
    url = obj.get('url')
    if url and url.endswith('.pdf'):
        ans = _process_pdf_and_answer(url)
        submit_url = obj.get('submit') or None
        if not submit_url:
            try:
                forms = page.query_selector_all('form')
                if forms:
                    s = forms[0].get_attribute('action')
                    submit_url = urljoin(url, s) if s else None
            except Exception:
                submit_url = None
        submission = None
        if submit_url:
            submission = _submit_answer(submit_url, email, secret, ans)
        return {"url": url, "answer": ans, "submitted": submission}
    return {"unhandled_obj": obj}

def _process_pdf_and_answer(pdf_url: str):
    r = httpx.get(pdf_url, timeout=60.0)
    r.raise_for_status()
    path = "tmp_download.pdf"
    with open(path, 'wb') as f:
        f.write(r.content)

    with pdfplumber.open(path) as pdf:
        if len(pdf.pages) >= 2:
            page = pdf.pages[1]
            tables = page.extract_tables()
            for t in tables:
                df = pd.DataFrame(t[1:], columns=t[0])
                cols = [c.strip().lower() for c in df.columns]
                df.columns = cols
                if 'value' in df.columns:
                    df['value'] = pd.to_numeric(df['value'].str.replace(',', ''), errors='coerce')
                    s = df['value'].sum(skipna=True)
                    return s if not pd.isna(s) else None
    return None

def _submit_answer(submit_url: str, email: str, secret: str, answer):
    payload = {
        "email": email,
        "secret": secret,
        "url": submit_url,
        "answer": answer,
    }
    r = httpx.post(submit_url, json=payload, timeout=60.0)
    try:
        r.raise_for_status()
        return r.json()
    except Exception:
        return {"status_code": r.status_code, "text": r.text}
