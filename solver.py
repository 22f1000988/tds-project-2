# solver.py
"""
Robust solver:
 - Canonical credentials only for solver submissions.
 - Route handler rewrites any page-originated POST body JSON 'secret' -> canonical_secret
   and continues the request. If rewriting fails, abort and record.
 - Keeps fetch/XHR init-script as an extra defense.
 - Records page requests and blocked requests for debugging.
"""

import re
import json
import base64
import asyncio
import time
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urljoin, urlparse

import httpx
import pandas as pd
import pdfplumber
from playwright.async_api import async_playwright, Route, Request, Response

# ----- helpers (same as before) -----
_PLACEHOLDER_TOKENS = {
    "your secret", "your-secret", "your_secret", "secret", "password", "your key", "$secret",
    "your-secret-here", "your-secret-value", "password123", "YOUR_SECRET", "SECRET",
    "your email", "your-email", "email@example.com", "youremail@domain"
}

def _looks_like_placeholder(s: Optional[str]) -> bool:
    if not s or not isinstance(s, str):
        return False
    return s.strip().lower() in _PLACEHOLDER_TOKENS

def _mask_secret_preview(s: Optional[str]) -> Optional[str]:
    if not s or not isinstance(s, str):
        return None
    return (s[:6] + "...") if len(s) > 6 else s

def _ensure_credentials(email: Optional[str], secret: Optional[str], result: Dict[str, Any]) -> Tuple[str, str]:
    if not email or not isinstance(email, str) or email.strip() == "":
        raise ValueError("Missing canonical email in payload")
    if not secret or not isinstance(secret, str) or secret.strip() == "":
        raise ValueError("Missing canonical secret in payload")

    used_email = email.strip()
    used_secret = secret.strip()
    result["used_email"] = used_email
    result["used_secret_preview"] = _mask_secret_preview(used_secret)

    if _looks_like_placeholder(used_email):
        result["warning"] = result.get("warning", []) + ["canonical_email_looks_like_placeholder"]
    if _looks_like_placeholder(used_secret):
        result["warning"] = result.get("warning", []) + ["canonical_secret_looks_like_placeholder"]

    return used_email, used_secret

def _process_pdf_bytes_sync(content_bytes: bytes) -> Optional[Any]:
    from io import BytesIO
    bio = BytesIO(content_bytes)
    try:
        with pdfplumber.open(bio) as pdf:
            pages_to_try = [1, 0] if len(pdf.pages) > 1 else [0]
            for idx in pages_to_try:
                try:
                    page = pdf.pages[idx]
                except Exception:
                    continue
                tables = page.extract_tables()
                for t in tables:
                    try:
                        df = pd.DataFrame(t[1:], columns=t[0])
                    except Exception:
                        continue
                    cols = [c.strip().lower() for c in df.columns]
                    df.columns = cols
                    if "value" in df.columns:
                        df["value"] = pd.to_numeric(df["value"].astype(str).str.replace(",", ""), errors="coerce")
                        s = df["value"].sum(skipna=True)
                        if pd.notna(s):
                            return int(s) if float(s).is_integer() else s
            text_concat = "\n".join(p.extract_text() or "" for p in pdf.pages)
            nums = re.findall(r"[-+]?[0-9]*\.?[0-9]+", text_concat)
            if nums:
                vals = [float(x) for x in nums]
                s = sum(vals)
                return int(s) if float(s).is_integer() else s
    except Exception:
        return None
    return None

def _parse_csv_text_sync(text_csv: str) -> Optional[Any]:
    from io import StringIO
    try:
        df = pd.read_csv(StringIO(text_csv))
    except Exception:
        return None
    cols = [c.strip().lower() for c in df.columns]
    df.columns = cols
    if "value" in df.columns:
        df["value"] = pd.to_numeric(df["value"].astype(str).str.replace(",", ""), errors="coerce")
        s = df["value"].sum(skipna=True)
        if pd.notna(s):
            return int(s) if float(s).is_integer() else s
    return None

def _maybe_decode_atob_text_sync(text: str) -> Optional[str]:
    if not text:
        return None
    m = re.search(r"atob\(`([A-Za-z0-9+/=\n]{20,})`\)", text)
    if m:
        try:
            return base64.b64decode(m.group(1)).decode("utf-8", errors="replace")
        except Exception:
            pass
    m2 = re.search(r"([A-Za-z0-9+/=\n]{200,})", text)
    if m2:
        try:
            return base64.b64decode(m2.group(1)).decode("utf-8", errors="replace")
        except Exception:
            pass
    return None

async def _maybe_decode_atob_text(text: str) -> Optional[str]:
    return _maybe_decode_atob_text_sync(text)

def _looks_like_url(s: str) -> bool:
    if not s or not isinstance(s, str):
        return False
    s = s.strip()
    if s.startswith("/"):
        return True
    parsed = urlparse(s)
    return bool(parsed.scheme in ("http", "https") and parsed.netloc)

def _extract_submit_candidate_from_text_sync(content_text: str) -> Optional[str]:
    if not content_text:
        return None
    m = re.search(r"(https?://[^\s'\"<>]*?/submit[^\s'\"<>]*)", content_text, flags=re.I)
    if m:
        return m.group(1).strip()
    m2 = re.search(r"(/[^\s'\"<>]*?submit[^\s'\"<>]*)", content_text, flags=re.I)
    if m2:
        return m2.group(1).strip()
    m3 = re.search(r"(?:POST|Post|post).*?(https?://[^\s'\"<>]+)", content_text, flags=re.I)
    if m3 and _looks_like_url(m3.group(1)):
        return m3.group(1).strip()
    m4 = re.search(r"(?:POST|Post|post).*?(/[^\\s'\"<>]+)", content_text, flags=re.I)
    if m4 and _looks_like_url(m4.group(1)):
        return m4.group(1).strip()
    m5 = re.search(r"(https?://[^\s'\"<>]+)", content_text)
    if m5:
        cand = m5.group(1).strip()
        idx = content_text.lower().find(cand.lower())
        window = content_text[max(0, idx-80): idx+len(cand)+80].lower() if idx >= 0 else ""
        if "submit" in window or "post your answer" in window:
            return cand
    return None

def _try_extract_json_from_text_sync(content_text: str) -> Optional[Dict[str, Any]]:
    if not content_text:
        return None
    m = re.search(r"(\{[\s\S]{10,4000}\})", content_text)
    if not m:
        return None
    candidate = m.group(1)
    try:
        return json.loads(candidate)
    except Exception:
        return None

def _parse_instruction_cutoff(text: str) -> Optional[float]:
    if not text:
        return None
    m = re.search(r"Cutoff[:\s]+([0-9]+(?:\.[0-9]+)?)", text, flags=re.I)
    if m:
        try:
            return float(m.group(1))
        except Exception:
            return None
    return None

def _apply_cutoff_to_dataframe(df: pd.DataFrame, cutoff: float, value_col: str = "value"):
    if value_col not in df.columns:
        return None
    try:
        arr = pd.to_numeric(df[value_col].astype(str).str.replace(",", ""), errors="coerce")
        filtered = arr[arr >= cutoff]
        if filtered.empty:
            return 0
        s = filtered.sum(skipna=True)
        return int(s) if float(s).is_integer() else s
    except Exception:
        return None

# ----- centralized submit (full payload logging) -----
async def _submit_answer(submit_url: str, canonical_email: str, canonical_secret: str, answer: Any, source_url: Optional[str] = None, timeout: float = 10.0) -> Dict[str, Any]:
    payload = {
        "email": canonical_email,
        "secret": canonical_secret,
        "url": source_url if source_url else submit_url,
        "answer": answer,
    }
    # print full payload for debugging (server will get this)
    print("[submit] POST", submit_url)
    print("[submit] payload:", json.dumps(payload, ensure_ascii=False))
    async with httpx.AsyncClient(timeout=timeout) as client:
        try:
            r = await client.post(submit_url, json=payload)
            text = r.text
            try:
                r.raise_for_status()
                try:
                    return r.json()
                except Exception:
                    return {"status_code": r.status_code, "text": text}
            except Exception:
                return {"status_code": r.status_code, "text": text}
        except Exception as e:
            return {"error": str(e)}

# ----- main solver with route rewrite + logging -----
async def solve_quiz_from_url(payload: Dict[str, Any], timeout_seconds: int = 60) -> Dict[str, Any]:
    canonical_email = payload.get("email")
    canonical_secret = payload.get("secret")
    url = payload.get("url")
    if not url:
        raise ValueError("No url provided in payload")

    result: Dict[str, Any] = {"url": url}
    canonical_email, canonical_secret = _ensure_credentials(canonical_email, canonical_secret, result)

    result["page_requests"] = []
    result["blocked_page_submissions"] = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox"])
        page = await browser.new_page()

        # init script to patch fetch/XHR (extra defense)
        js_secret_json = json.dumps(canonical_secret)
        init_script = (
            "window.__CANONICAL_SECRET = %s;\n"
            "(function(){\n"
            "  const origFetch = window.fetch;\n"
            "  window.fetch = function(input, init) {\n"
            "    try {\n"
            "      if (init && init.method && init.method.toUpperCase() === 'POST' && init.body) {\n"
            "        try {\n"
            "          let body = init.body;\n"
            "          if (typeof body === 'string') {\n"
            "            try {\n"
            "              let obj = JSON.parse(body);\n"
            "              if (obj && typeof obj === 'object') {\n"
            "                obj.secret = window.__CANONICAL_SECRET;\n"
            "                init.body = JSON.stringify(obj);\n"
            "              }\n"
            "            } catch(e){}\n"
            "          }\n"
            "        } catch(e){}\n"
            "      }\n"
            "    } catch(e){}\n"
            "    return origFetch(input, init);\n"
            "  };\n"
            "  const XHProto = window.XMLHttpRequest && window.XMLHttpRequest.prototype;\n"
            "  if (XHProto) {\n"
            "    const _send = XHProto.send;\n"
            "    XHProto.send = function(body) {\n"
            "      try {\n"
            "        if (body && typeof body === 'string') {\n"
            "          try {\n"
            "            let obj = JSON.parse(body);\n"
            "            if (obj && typeof obj === 'object') {\n"
            "              obj.secret = window.__CANONICAL_SECRET;\n"
            "              body = JSON.stringify(obj);\n"
            "            }\n"
            "          } catch(e){}\n"
            "        }\n"
            "      } catch(e){}\n"
            "      return _send.call(this, body);\n"
            "    };\n"
            "  }\n"
            "})();\n"
        ) % js_secret_json
        await page.add_init_script(init_script)

        # Detailed request listener: record start
        async def _on_request(req: Request):
            try:
                entry = {"url": req.url, "method": req.method, "headers": dict(req.headers)}
                post = None
                try:
                    pd = await req.post_data()
                    post = pd
                except Exception:
                    post = None
                entry["post_data_preview"] = (post[:200] if isinstance(post, str) else None)
                result["page_requests"].append(entry)
            except Exception:
                pass

        page.on("request", _on_request)

        # Route: rewrite page-originated /submit POST bodies to canonical_secret, else abort if cannot
        async def _route_handler(route: Route, request: Request):
            req_url = request.url
            method = request.method.upper()
            # If page-originated POST to a submit-like path
            if method == "POST" and "/submit" in req_url:
                body_preview = None
                try:
                    post_raw = await request.post_data()
                    body_preview = post_raw if post_raw else None
                except Exception:
                    body_preview = "<unreadable>"

                # ----- BEGIN REPLACED: robust multi-encoding rewrite/abort -----
                # Try to parse a variety of body encodings and replace secret where possible
                rewritten = False
                try:
                    # fetch raw post data if available
                    post_raw = await request.post_data()
                except Exception:
                    post_raw = None

                # normalize raw -> text (if possible)
                raw_text = None
                if isinstance(post_raw, str):
                    raw_text = post_raw
                elif isinstance(post_raw, (bytes, bytearray)):
                    try:
                        raw_text = post_raw.decode("utf-8", errors="replace")
                    except Exception:
                        raw_text = None
                else:
                    raw_text = None

                content_type = (request.headers.get("content-type") or request.headers.get("Content-Type") or "").lower()

                # 1) JSON body (string) -> rewrite secret
                if raw_text:
                    try:
                        parsed = json.loads(raw_text)
                        if isinstance(parsed, dict):
                            parsed["secret"] = canonical_secret
                            new_body = json.dumps(parsed)
                            headers = dict(request.headers)
                            headers["content-type"] = "application/json"
                            print("[route] rewriting page POST to /submit (json), url:", req_url)
                            result.setdefault("page_requests_rewritten", []).append({
                                "url": req_url,
                                "original_preview": (raw_text[:1000] if raw_text else None),
                                "rewritten_preview": new_body[:1000]
                            })
                            try:
                                await route.continue_(post_data=new_body, headers=headers)
                                rewritten = True
                                return
                            except Exception as e:
                                print("[route] continue_ failed for json rewrite:", str(e))
                                rewritten = False
                    except Exception:
                        # not JSON, try other encodings
                        pass

                # 2) application/x-www-form-urlencoded -> parse and rewrite secret
                if "application/x-www-form-urlencoded" in content_type and raw_text is not None:
                    try:
                        from urllib.parse import parse_qs, urlencode
                        qs = parse_qs(raw_text, keep_blank_values=True)
                        # only rewrite if secret present (or even if absent, add it)
                        qs["secret"] = [canonical_secret]
                        flat = {k: (v[0] if isinstance(v, list) and v else "") for k, v in qs.items()}
                        new_body = urlencode(flat)
                        headers = dict(request.headers)
                        headers["content-type"] = "application/x-www-form-urlencoded"
                        print("[route] rewriting page POST to /submit (form-urlencoded), url:", req_url)
                        result.setdefault("page_requests_rewritten", []).append({
                            "url": req_url,
                            "original_preview": (raw_text[:1000] if raw_text else None),
                            "rewritten_preview": new_body[:1000]
                        })
                        try:
                            await route.continue_(post_data=new_body, headers=headers)
                            rewritten = True
                            return
                        except Exception as e:
                            print("[route] continue_ failed for form rewrite:", str(e))
                            rewritten = False
                    except Exception as e:
                        print("[route] failed to parse form-urlencoded body:", str(e))
                        rewritten = False

                # 3) multipart/form-data (files) - cannot safely rewrite reliably; log & abort
                if "multipart/form-data" in content_type:
                    log_entry = {"url": req_url, "method": method, "body_preview": (raw_text[:1000] if raw_text else None), "rewritten": False, "reason": "multipart-cannot-rewrite"}
                    print("[route] aborting page-initiated submit request (multipart form, cannot rewrite):", log_entry)
                    result.setdefault("blocked_page_submissions", []).append(log_entry)
                    try:
                        await route.abort()
                    except Exception:
                        try:
                            await route.continue_()
                        except Exception:
                            pass
                    return

                # If no rewrite succeeded above, abort to prevent placeholder submission
                log_entry = {"url": req_url, "method": method, "body_preview": (raw_text[:1000] if raw_text else None), "rewritten": rewritten}
                print("[route] aborting page-initiated submit request (couldn't rewrite):", log_entry)
                result.setdefault("blocked_page_submissions", []).append(log_entry)
                try:
                    await route.abort()
                except Exception:
                    try:
                        await route.continue_()
                    except Exception:
                        pass
                return
                # ----- END REPLACED BLOCK -----

            # otherwise continue normally
            try:
                await route.continue_()
            except Exception:
                try:
                    await route.continue_()
                except Exception:
                    pass

        await page.route("**/*", _route_handler)

        # Navigate
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=15_000)
        except Exception as e:
            result["navigation_error"] = str(e)

        try:
            content = await page.content()
        except Exception:
            content = ""
        try:
            text = await page.evaluate("() => document.body ? document.body.innerText : document.documentElement.innerText")
        except Exception:
            text = content or ""

        result["rendered_text_snippet"] = (text or "")[:2000]

        # --- existing parsing and submission logic ---
        # Try decode atob/base64 JSON embedded
        decoded = await _maybe_decode_atob_text(content) or await _maybe_decode_atob_text(text)
        if decoded:
            jq = re.search(r"\{[\s\S]*\}", decoded)
            if jq:
                try:
                    quiz_obj = json.loads(jq.group(0))
                    result["decoded_quiz_object"] = quiz_obj
                    if isinstance(quiz_obj, dict):
                        if "email" in quiz_obj:
                            result["ignored_page_email"] = quiz_obj.get("email")
                        if "secret" in quiz_obj:
                            result["ignored_page_secret"] = quiz_obj.get("secret")
                    data_url = quiz_obj.get("url")
                    submit_field = quiz_obj.get("submit") or quiz_obj.get("submit_url") or quiz_obj.get("submitUrl")
                    if data_url:
                        if data_url.startswith("/"):
                            data_url = urljoin(url, data_url)
                        if data_url.lower().endswith(".csv"):
                            try:
                                async with httpx.AsyncClient(timeout=15.0) as client:
                                    r = await client.get(data_url)
                                    r.raise_for_status()
                                    try:
                                        ans = await asyncio.wait_for(asyncio.to_thread(_parse_csv_text_sync, r.text), timeout=5.0)
                                    except asyncio.TimeoutError:
                                        ans = None
                                        result["csv_processing_error"] = "timeout"
                                    result["answer"] = ans
                                    if submit_field and ans is not None:
                                        final_submit = urljoin(url, submit_field) if submit_field.startswith("/") else submit_field
                                        submission = await _submit_answer(final_submit, canonical_email, canonical_secret, ans, source_url=url)
                                        result["submitted"] = submission
                                    await browser.close()
                                    return result
                            except Exception as e:
                                result["data_fetch_error"] = str(e)
                        if data_url.lower().endswith(".pdf"):
                            try:
                                async with httpx.AsyncClient(timeout=20.0) as client:
                                    r = await client.get(data_url)
                                    r.raise_for_status()
                                    try:
                                        ans = await asyncio.wait_for(asyncio.to_thread(_process_pdf_bytes_sync, r.content), timeout=20.0)
                                    except asyncio.TimeoutError:
                                        ans = None
                                        result["pdf_processing_error"] = "timeout"
                                    result["answer"] = ans
                                    if submit_field and ans is not None:
                                        final_submit = urljoin(url, submit_field) if submit_field.startswith("/") else submit_field
                                        submission = await _submit_answer(final_submit, canonical_email, canonical_secret, ans, source_url=url)
                                        result["submitted"] = submission
                                    await browser.close()
                                    return result
                            except Exception as e:
                                result["data_fetch_error"] = str(e)
                except Exception:
                    pass

        submit_candidate = _extract_submit_candidate_from_text_sync(content) or _extract_submit_candidate_from_text_sync(text)
        if submit_candidate:
            cand = submit_candidate.strip()
            if _looks_like_url(cand):
                submit_url = urljoin(url, cand) if cand.startswith("/") else cand
            else:
                result["ignored_submit_candidate"] = cand
            result["found_submit_candidate"] = submit_candidate
            result["resolved_submit_url"] = submit_url

        # find file links and parse CSV/PDF (same robust logic)
        try:
            anchors = await page.evaluate("() => Array.from(document.querySelectorAll('a')).map(a => a.href)")
        except Exception:
            anchors = []
        file_links = [a for a in anchors if a and any(ext in a.lower() for ext in [".pdf", ".csv", ".xlsx"])]
        if file_links:
            csvs = [l for l in file_links if l.lower().endswith(".csv")]
            pdfs = [l for l in file_links if l.lower().endswith(".pdf")]
            if csvs:
                csv_url = csvs[0]
                result["found_csv"] = csv_url
                try:
                    async with httpx.AsyncClient(timeout=15.0) as client:
                        r = await client.get(csv_url)
                        r.raise_for_status()
                        preview_text = (r.text or "")[:1000]
                        result["csv_preview"] = preview_text
                        cutoff = _parse_instruction_cutoff(text) or _parse_instruction_cutoff(content)
                        def _try_parse_csv_and_rules(csv_text: str, cutoff_val: Optional[float]):
                            from io import StringIO
                            try:
                                df = pd.read_csv(StringIO(csv_text))
                            except Exception:
                                try:
                                    df = pd.read_csv(StringIO(csv_text), encoding="latin1")
                                except Exception:
                                    return {"error": "could_not_parse_csv"}

                            colnames = [str(c).strip() for c in df.columns]
                            all_cols_numeric_like = all(re.match(r"^-?\d+(\.\d+)?$", cn) for cn in colnames)
                            single_numeric_header = (len(colnames) == 1 and re.match(r"^-?\d+(\.\d+)?$", colnames[0]))

                            if all_cols_numeric_like or single_numeric_header:
                                try:
                                    df = pd.read_csv(StringIO(csv_text), header=None, names=["value"])
                                except Exception:
                                    try:
                                        df = pd.read_csv(StringIO(csv_text), header=None, names=["value"], encoding="latin1")
                                    except Exception:
                                        return {"error": "could_not_parse_headerless_csv"}

                            df.columns = [str(c).strip().lower() for c in df.columns]

                            candidates = [c for c in df.columns if c in ("value", "amount", "amt", "score", "v", "val")]
                            if not candidates:
                                for c in df.columns:
                                    sample = df[c].astype(str).dropna().head(20).astype(str).tolist()
                                    numeric_count = sum(1 for s in sample if re.search(r"[0-9]", s))
                                    if numeric_count >= max(1, len(sample)//2):
                                        candidates.append(c)
                                        break
                            if not candidates:
                                return {"error": "no_numeric_column_found", "columns": list(df.columns)}

                            chosen_col = candidates[0]

                            def clean_num(x):
                                if x is None:
                                    return None
                                s = str(x).strip()
                                if s == "":
                                    return None
                                s = re.sub(r"[,\s₹£€$]", "", s)
                                if re.match(r"^\(.*\)$", s):
                                    s = "-" + s.strip("()")
                                m = re.search(r"[-+]?[0-9]*\.?[0-9]+", s)
                                if m:
                                    try:
                                        return float(m.group(0))
                                    except Exception:
                                        return None
                                return None

                            numeric_series = df[chosen_col].apply(clean_num)
                            preview_nums = numeric_series.dropna().head(10).tolist()

                            out = {"chosen_column": chosen_col, "sample_numbers": preview_nums}

                            if cutoff_val is not None:
                                above = numeric_series[numeric_series >= cutoff_val].dropna()
                                out["cutoff"] = cutoff_val
                                out["sum_above_cutoff"] = int(above.sum()) if not above.empty and float(above.sum()).is_integer() else (above.sum() if not above.empty else 0)
                                out["count_above_cutoff"] = int(above.count())
                                out["answer"] = out["sum_above_cutoff"]
                                return out

                            total = numeric_series.dropna().sum()
                            out["sum_total"] = int(total) if float(total).is_integer() else total
                            out["answer"] = out["sum_total"]
                            return out

                        try:
                            parsed_info = await asyncio.wait_for(asyncio.to_thread(_try_parse_csv_and_rules, r.text, cutoff), timeout=12.0)
                        except asyncio.TimeoutError:
                            parsed_info = {"error": "csv_parse_timeout"}

                        result["csv_parsed_info"] = parsed_info
                        ans = None
                        if isinstance(parsed_info, dict) and "answer" in parsed_info:
                            ans = parsed_info["answer"]
                        result["answer"] = ans

                        if submit_url and ans is not None:
                            submission = await _submit_answer(submit_url, canonical_email, canonical_secret, ans, source_url=url)
                            result["submitted"] = submission
                        elif submit_url and ans is None:
                            result["note"] = "Found submit_url but no computed answer; skipping submission to avoid 404"
                        await browser.close()
                        return result
                except Exception as e:
                    result["csv_fetch_error"] = str(e)
            if pdfs:
                pdf_url = pdfs[0]
                result["found_pdf"] = pdf_url
                try:
                    async with httpx.AsyncClient(timeout=20.0) as client:
                        r = await client.get(pdf_url)
                        r.raise_for_status()
                        try:
                            ans = await asyncio.wait_for(asyncio.to_thread(_process_pdf_bytes_sync, r.content), timeout=20.0)
                        except asyncio.TimeoutError:
                            ans = None
                            result["pdf_processing_error"] = "timeout"
                        result["answer"] = ans
                        if submit_url and ans is not None:
                            submission = await _submit_answer(submit_url, canonical_email, canonical_secret, ans, source_url=url)
                            result["submitted"] = submission
                        elif submit_url and ans is None:
                            result["note"] = "Found submit_url but no computed answer; skipping submission to avoid 404"
                        await browser.close()
                        return result
                except Exception as e:
                    result["pdf_fetch_error"] = str(e)

        # 4) DOM tables
        try:
            tables = await page.query_selector_all("table")
        except Exception:
            tables = []
        if tables:
            for t in tables:
                try:
                    rows = await t.evaluate("(node) => Array.from(node.querySelectorAll('tr')).map(r => Array.from(r.querySelectorAll('th,td')).map(c=>c.innerText))")
                except Exception:
                    continue
                if not rows or len(rows) < 2:
                    continue
                header = [c.strip().lower() for c in rows[0]]
                if "value" in header:
                    idx = header.index("value")
                    s = 0.0
                    got = False
                    for rrow in rows[1:]:
                        try:
                            v = float(str(rrow[idx]).replace(",", ""))
                            s += v
                            got = True
                        except Exception:
                            continue
                    if got:
                        ans = int(s) if float(s).is_integer() else s
                        result["table_sum"] = ans
                        result["table_header"] = header
                        if submit_url and ans is not None:
                            submission = await _submit_answer(submit_url, canonical_email, canonical_secret, ans, source_url=url)
                            result["submitted"] = submission
                        elif submit_url and ans is None:
                            result["note"] = "Found submit_url but no computed answer; skipping submission to avoid 404"
                        await browser.close()
                        return result

        # 5) JSON example on page
        json_example = _try_extract_json_from_text_sync(content) or _try_extract_json_from_text_sync(text)
        if json_example and isinstance(json_example, dict):
            result["json_example"] = json_example
            if "email" in json_example:
                result["ignored_page_email"] = json_example.get("email")
            if "secret" in json_example:
                result["ignored_page_secret"] = json_example.get("secret")
            candidate_data_url = json_example.get("url")
            if candidate_data_url:
                if candidate_data_url.startswith("/"):
                    candidate_data_url = urljoin(url, candidate_data_url)
                if candidate_data_url.lower().endswith(".csv"):
                    try:
                        async with httpx.AsyncClient(timeout=15.0) as client:
                            r = await client.get(candidate_data_url)
                            r.raise_for_status()
                            cutoff = _parse_instruction_cutoff(text) or _parse_instruction_cutoff(content)
                            def _parse_csv_and_apply_rules_inner(csv_text: str, cutoff_val: Optional[float]):
                                from io import StringIO
                                try:
                                    df = pd.read_csv(StringIO(csv_text))
                                except Exception:
                                    return None
                                df.columns = [c.strip().lower() for c in df.columns]
                                if cutoff_val is not None:
                                    return _apply_cutoff_to_dataframe(df, cutoff_val, value_col="value")
                                if "value" in df.columns:
                                    arr = pd.to_numeric(df["value"].astype(str).str.replace(",", ""), errors="coerce")
                                    s = arr.sum(skipna=True)
                                    return int(s) if float(s).is_integer() else s
                                return None
                            try:
                                ans = await asyncio.wait_for(asyncio.to_thread(_parse_csv_and_apply_rules_inner, r.text, cutoff), timeout=8.0)
                            except asyncio.TimeoutError:
                                ans = None
                                result["csv_processing_error"] = "timeout"
                            result["answer"] = ans
                            submit_candidate_field = json_example.get("submit") or json_example.get("submit_url") or json_example.get("submitUrl")
                            final_submit = None
                            if submit_candidate_field:
                                final_submit = urljoin(url, submit_candidate_field) if submit_candidate_field.startswith("/") else submit_candidate_field
                            elif submit_url:
                                final_submit = submit_url
                            if final_submit and ans is not None:
                                submission = await _submit_answer(final_submit, canonical_email, canonical_secret, ans, source_url=url)
                                result["submitted"] = submission
                            elif final_submit and ans is None:
                                result["note"] = "Found final_submit but no computed answer; skipping submission"
                            await browser.close()
                            return result
                    except Exception as e:
                        result["json_fetch_error"] = str(e)
                if candidate_data_url.lower().endswith(".pdf"):
                    try:
                        async with httpx.AsyncClient(timeout=20.0) as client:
                            r = await client.get(candidate_data_url)
                            r.raise_for_status()
                            try:
                                ans = await asyncio.wait_for(asyncio.to_thread(_process_pdf_bytes_sync, r.content), timeout=20.0)
                            except asyncio.TimeoutError:
                                ans = None
                                result["pdf_processing_error"] = "timeout"
                            result["answer"] = ans
                            final_submit = (json_example.get("submit") or json_example.get("submit_url") or submit_url)
                            if final_submit:
                                final_submit = urljoin(url, final_submit) if isinstance(final_submit, str) and final_submit.startswith("/") else final_submit
                                if ans is not None:
                                    submission = await _submit_answer(final_submit, canonical_email, canonical_secret, ans, source_url=url)
                                    result["submitted"] = submission
                                else:
                                    result["note"] = "Found final_submit but no computed answer; skipping submission"
                            await browser.close()
                            return result
                    except Exception as e:
                        result["json_fetch_error"] = str(e)

        # 6) fallback: submit only when we have computed answer
        if submit_url:
            placeholder_answer = result.get("answer") if "answer" in result else None
            if placeholder_answer is None:
                placeholder = None
                je = result.get("json_example")
                if isinstance(je, dict):
                    ans_example = je.get("answer")
                    if isinstance(ans_example, str):
                        low = ans_example.strip().lower()
                        if "anything" in low or "your answer" in low or "the secret code" in low or "..." in low:
                            placeholder = ans_example
                if placeholder is None:
                    dq = result.get("decoded_quiz_object")
                    if isinstance(dq, dict):
                        ans_example = dq.get("answer")
                        if isinstance(ans_example, str):
                            low = ans_example.strip().lower()
                            if "anything" in low or "your answer" in low or "the secret code" in low or "..." in low:
                                placeholder = ans_example
                if placeholder is None:
                    page_text = (result.get("rendered_text_snippet") or "").lower()
                    if any(phr in page_text for phr in ("anything you want", "post this json", "post your answer", "post this json to", "\"answer\": \"anything")):
                        placeholder = "anything you want"
                if placeholder is None:
                    result["note"] = "submit_url found but no computed answer; skipping submission"
                    await browser.close()
                    return result
                submission = await _submit_answer(submit_url, canonical_email, canonical_secret, placeholder, source_url=url)
                result["submitted"] = submission
                await browser.close()
                return result
            else:
                submission = await _submit_answer(submit_url, canonical_email, canonical_secret, placeholder_answer, source_url=url)
                result["submitted"] = submission
                await browser.close()
                return result

        # 7) forms
        try:
            forms = await page.query_selector_all("form")
            for f in forms:
                a = await f.get_attribute("action")
                if a:
                    resolved = a if a.lower().startswith("http") else urljoin(url, a)
                    result["form_action"] = resolved
                    await browser.close()
                    return result
        except Exception:
            pass

        await browser.close()
        result["status"] = "no-handled-case"
        result["submit_url"] = submit_url
        return result

# end of solver.py
