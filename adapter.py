"""
DeepSeek Chat API Adapter - WASM-based PoW solving, session management, streaming
Supports expert mode (thinking_enabled, search_enabled).

Anti-detection upgrades (2026-06):
- TLS/JA3 impersonation via curl_cffi (chrome131)
- Header set & values captured from a real Chrome 149 + chat.deepseek.com session
- Cookie jar auto-rotates Set-Cookie (cf_clearance / awswaf token refresh)
- Detects 405 x-amzn-waf-action=captcha and 403/429 cf-mitigated=challenge
"""
import json
import os
import time
import struct
import base64
import random
import threading
from pathlib import Path
from dotenv import load_dotenv
from wasmtime import Store, Module, Instance

try:
    from curl_cffi import requests as cffi_requests
except ImportError as e:
    raise SystemExit(
        "curl_cffi is required for TLS fingerprint impersonation.\n"
        "Run: pip install -r requirements.txt"
    ) from e

load_dotenv()

COOKIES = os.environ.get("DEEPSEEK_COOKIES", "")
BASE_URL = "https://chat.deepseek.com"
TOKEN = os.environ.get("DEEPSEEK_TOKEN", "")
IMPERSONATE = os.environ.get("DEEPSEEK_IMPERSONATE", "chrome131")
try:
    JITTER_SECS = max(0.0, float(os.environ.get("DEEPSEEK_JITTER_SECS", "0") or 0))
except ValueError:
    JITTER_SECS = 0.0

_WASM_PATH = Path(__file__).resolve().parent / "sha3_wasm_bg.wasm"
with open(_WASM_PATH, "rb") as f:
    _WASM_BYTES = f.read()


class WASMError(Exception):
    pass


class PoWError(Exception):
    pass


class WAFChallengeError(Exception):
    """Raised when AWS WAF or Cloudflare returns a challenge response."""
    def __init__(self, kind: str, status: int, body: str = ""):
        super().__init__(f"{kind} challenge ({status}): {body[:200]}")
        self.kind = kind
        self.status = status
        self.body = body


class _WASMSolver:
    """WASM-based PoW solver (reused across calls) — thread-safe via lock."""

    def __init__(self):
        self._lock = threading.Lock()
        self.store = Store()
        module = Module(self.store.engine, _WASM_BYTES)
        instance = Instance(self.store, module, [])
        exports = instance.exports(self.store)
        self.memory = exports["memory"]
        self.wasm_solve = exports["wasm_solve"]
        self.add_to_stack = exports["__wbindgen_add_to_stack_pointer"]
        self.malloc = exports["__wbindgen_export_0"]
        self._wbindgen_free = exports["__wbindgen_export_2"]
        self._allocations: list[tuple[int, int]] = []

    def _encode(self, s: str):
        data = s.encode("utf-8")
        ptr = self.malloc(self.store, len(data), 1)
        mem = self.memory.data_ptr(self.store)
        for i, b in enumerate(data):
            mem[ptr + i] = b
        self._allocations.append((ptr, len(data)))
        return ptr, len(data)

    def _free_allocations(self):
        for ptr, length in self._allocations:
            try:
                self._wbindgen_free(self.store, ptr, length, 1)
            except Exception:
                pass
        self._allocations.clear()

    def solve(self, challenge: str, salt: str, expire_at: int, difficulty: int) -> int:
        with self._lock:
            try:
                prefix = f"{salt}_{expire_at}_"
                stack_ptr = self.add_to_stack(self.store, -16)
                chal_ptr, chal_len = self._encode(challenge)
                prefix_ptr, prefix_len = self._encode(prefix)
                self.wasm_solve(self.store, stack_ptr, chal_ptr, chal_len,
                                prefix_ptr, prefix_len, float(difficulty))
                mem = self.memory.data_ptr(self.store)
                ret = int.from_bytes(bytes(mem[stack_ptr:stack_ptr + 4]),
                                     byteorder='little', signed=True)
                if ret == 0:
                    raise PoWError("WASM solver found no solution")
                result = struct.unpack('<d', bytes(mem[stack_ptr + 8:stack_ptr + 16]))[0]
                self.add_to_stack(self.store, 16)
                return int(result)
            finally:
                self._free_allocations()


class DeepSeekAdapter:
    """Adapter for DeepSeek Chat API"""

    # Captured 2026-06-19 from a live Chrome 149 session on chat.deepseek.com.
    # We DOWNGRADE the UA string to Chrome 131 to match what curl_cffi's
    # `chrome131` impersonation profile actually negotiates at the TLS layer:
    # the JA3/JA4 fingerprint comes from a Chrome 131 build, so a Chrome 149
    # UA on a Chrome 131 ClientHello is itself a fingerprint mismatch. Bump
    # both together when curl_cffi adds newer chrome profiles.
    _DEFAULT_UA = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    )
    _DEFAULT_SEC_CH_UA = (
        '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"'
    )

    def __init__(self, token: str = TOKEN, cookies: str = COOKIES,
                 impersonate: str = IMPERSONATE, proxy: str | None = None):
        self.token = self._normalize_token(token)
        self.cookies = cookies
        self.impersonate = impersonate
        self._solver = None
        # curl_cffi.Session keeps a cookie jar that auto-merges Set-Cookie,
        # so cf_clearance / AWS WAF tokens stay fresh across calls.
        self._client = cffi_requests.Session(
            impersonate=impersonate,
            timeout=120,
            proxies={"all": proxy} if proxy else None,
        )
        # Seed jar from the user-supplied cookie blob (one-shot import only;
        # afterwards the jar is the source of truth).
        if cookies:
            for part in cookies.split(";"):
                part = part.strip()
                if not part or "=" not in part:
                    continue
                name, value = part.split("=", 1)
                try:
                    self._client.cookies.set(
                        name.strip(), value.strip(), domain=".deepseek.com"
                    )
                except Exception:
                    pass

        self._msg_counters: dict[str, int] = {}
        # Header set captured from a live browser fetch().
        # Names use the same casing the real browser sends.
        self._base_headers = {
            "User-Agent": self._DEFAULT_UA,
            "Accept": "*/*",
            "Accept-Encoding": "gzip, deflate, br, zstd",
            "Accept-Language": "zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.7",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.token}",
            "Origin": BASE_URL,
            "Referer": f"{BASE_URL}/",
            "Priority": "u=1, i",
            "Sec-Ch-Ua": self._DEFAULT_SEC_CH_UA,
            "Sec-Ch-Ua-Mobile": "?0",
            "Sec-Ch-Ua-Platform": '"Windows"',
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
            # DeepSeek-specific application headers (current as of 2026-06-19).
            "X-App-Version": "2.0.0",
            "X-Client-Version": "2.0.0",
            "X-Client-Platform": "web",
            "X-Client-Locale": "zh_CN",
            "X-Client-Timezone-Offset": "28800",
            "x-client-bundle-id": "com.deepseek.chat",
        }

    @staticmethod
    def _normalize_token(token: str) -> str:
        """Accept either a bare token or DeepSeek's localStorage JSON wrapper.

        DeepSeek stores its token in localStorage as
            {"value":"<bare-token>","__version":"0"}
        but the network layer sends only the bare value as `Authorization:
        Bearer <bare-token>`. Users sometimes copy the localStorage form by
        mistake. Auto-unwrap it so the adapter accepts either form.
        """
        if not token:
            return token
        s = token.strip()
        if s.startswith("Bearer "):
            s = s[len("Bearer "):].strip()
        if s.startswith("{") and s.endswith("}"):
            try:
                obj = json.loads(s)
                if isinstance(obj, dict) and "value" in obj and isinstance(obj["value"], str):
                    return obj["value"]
            except (ValueError, TypeError):
                pass
        return s

    @staticmethod
    def _detect_waf_challenge(status: int, headers) -> str | None:
        """Return the challenge kind if the response is a WAF/CDN challenge."""
        get = headers.get if hasattr(headers, "get") else lambda k, d=None: dict(headers).get(k, d)
        waf_action = (get("x-amzn-waf-action") or "").lower()
        cf_mitigated = (get("cf-mitigated") or "").lower()
        if status == 405 and waf_action == "captcha":
            return "aws-waf-captcha"
        if status == 202 and waf_action == "challenge":
            return "aws-waf-challenge"
        if status in (403, 429) and cf_mitigated == "challenge":
            return "cloudflare-challenge"
        return None

    @property
    def solver(self):
        if self._solver is None:
            self._solver = _WASMSolver()
        return self._solver

    def _get_challenge(self, target_path: str = "/api/v0/chat/completion"):
        resp = self._client.post(
            f"{BASE_URL}/api/v0/chat/create_pow_challenge",
            json={"target_path": target_path},
            headers=self._base_headers,
        )
        kind = self._detect_waf_challenge(resp.status_code, resp.headers)
        if kind:
            raise WAFChallengeError(kind, resp.status_code, resp.text)
        resp.raise_for_status()
        data = resp.json()
        try:
            return data["data"]["biz_data"]["challenge"]
        except (KeyError, TypeError) as e:
            raise RuntimeError(
                f"Unexpected challenge response structure: {data.get('code', 'unknown')} - {data.get('msg', str(e))}"
            )

    def _solve(self, challenge_data: dict) -> str:
        nonce = self.solver.solve(
            challenge=challenge_data["challenge"],
            salt=challenge_data["salt"],
            expire_at=challenge_data["expire_at"],
            difficulty=challenge_data["difficulty"],
        )
        raw = json.dumps({
            "algorithm": "DeepSeekHashV1",
            "challenge": challenge_data["challenge"],
            "salt": challenge_data["salt"],
            "answer": nonce,
            "signature": challenge_data["signature"],
            "target_path": challenge_data["target_path"],
        }, separators=(",", ":"))
        return base64.b64encode(raw.encode()).decode()

    def _pow_headers(self, target_path: str = "/api/v0/chat/completion"):
        if JITTER_SECS > 0:
            time.sleep(random.uniform(0, JITTER_SECS))
        c = self._get_challenge(target_path)
        pow_h = self._solve(c)
        return {**self._base_headers, "X-DS-PoW-Response": pow_h}

    def create_session(self) -> str:
        """Create a new chat session, returns session_id"""
        headers = self._pow_headers("/api/v0/chat/completion")
        resp = self._client.post(
            f"{BASE_URL}/api/v0/chat_session/create",
            json={"target_path": "/api/v0/chat/completion"},
            headers=headers,
        )
        kind = self._detect_waf_challenge(resp.status_code, resp.headers)
        if kind:
            raise WAFChallengeError(kind, resp.status_code, resp.text)
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") != 0:
            raise RuntimeError(f"Session creation failed: {data}")
        biz = data["data"]["biz_data"]
        # Handle both formats: direct id vs nested chat_session.id
        if "id" in biz:
            return biz["id"]
        return biz["chat_session"]["id"]

    def _parse_sse(self, text: str):
        """Parse SSE text into a list of events"""
        events = []
        current_event = ""
        for line in text.split("\n"):
            if line.startswith("event: "):
                current_event = line[7:]
            elif line.startswith("data: "):
                data_str = line[6:]
                if data_str:
                    try:
                        events.append((current_event, json.loads(data_str)))
                    except json.JSONDecodeError:
                        events.append((current_event, data_str))
                current_event = ""
        return events

    def _send_completion(self, session_id: str, prompt: str, stream: bool = False,
                         model_type: str | None = None,
                         thinking_enabled: bool = False, search_enabled: bool = False):
        """Send a completion request, returns raw response"""
        headers = self._pow_headers("/api/v0/chat/completion")
        mid = self._msg_counters.get(session_id, 0) + 1
        self._msg_counters[session_id] = mid
        body = {
            "chat_session_id": session_id,
            "parent_message_id": mid - 1 if mid > 1 else None,
            "model_type": model_type,
            "prompt": prompt,
            "ref_file_ids": [],
            "stream": stream,
            "thinking_enabled": thinking_enabled,
            "search_enabled": search_enabled,
            "preempt": False,
        }
        resp = self._client.post(
            f"{BASE_URL}/api/v0/chat/completion",
            json=body,
            headers=headers,
        )
        kind = self._detect_waf_challenge(resp.status_code, resp.headers)
        if kind:
            raise WAFChallengeError(kind, resp.status_code, resp.text)
        resp.raise_for_status()
        return resp

    def chat(self, session_id: str, prompt: str, model_type: str | None = None,
             thinking_enabled: bool = False, search_enabled: bool = False) -> str:
        """Send a non-streaming chat message, returns response content."""
        resp = self._send_completion(session_id, prompt, stream=False,
                                      model_type=model_type,
                                      thinking_enabled=thinking_enabled,
                                      search_enabled=search_enabled)
        events = self._parse_sse(resp.text)

        # Collect all content from both normal mode and expert fragment mode
        content_parts = []
        thinking_parts = []
        frag_type = None  # None, 'thinking', 'content'

        for event_type, data in events:
            if not isinstance(data, dict):
                continue
            p = data.get("p", "")
            o = data.get("o", "")
            v = data.get("v", "")

            # Expert mode: initial response with fragments
            if isinstance(v, dict) and 'response' in v:
                resp_data = v['response']
                fragments = resp_data.get('fragments', [])
                if fragments:
                    ft = fragments[0].get('type', '')
                    frag_type = 'thinking' if ft == 'THINK' else 'content'
                    fc = fragments[0].get('content', '')
                    if fc:
                        (thinking_parts if frag_type == 'thinking' else content_parts).append(fc)
                continue

            # Expert mode: fragment content append
            if p == "response/fragments/-1/content" and o == "APPEND":
                if frag_type == 'thinking':
                    thinking_parts.append(v)
                else:
                    content_parts.append(v)
                continue
            if p == "response/fragments/-1/content" and not o:
                # Frag content without o (happens after fragment switch)
                if frag_type == 'thinking':
                    thinking_parts.append(v)
                else:
                    content_parts.append(v)
                continue

            # Expert mode: fragment switch
            if p == "response/fragments" and o == "APPEND":
                if isinstance(v, list) and v:
                    new_type = v[0].get('type', '')
                    if new_type == 'RESPONSE':
                        frag_type = 'content'
                    elif new_type == 'THINK':
                        frag_type = 'thinking'
                continue

            # Normal mode
            if p == "response/content" and o == "APPEND":
                content_parts.append(v)
                continue

            # Plain token event — belongs to current fragment or normal mode
            if "v" in data and "p" not in data and "o" not in data:
                token = data["v"]
                if isinstance(token, str) and token:
                    if frag_type == 'thinking':
                        thinking_parts.append(token)
                    else:
                        content_parts.append(token)
                continue

        return "".join(content_parts)

    def chat_stream(self, session_id: str, prompt: str,
                    model_type: str | None = None,
                    thinking_enabled: bool = False, search_enabled: bool = False):
        """Stream a chat message, yields content tokens.

        In expert mode (model_type='expert'), yields dicts with
        __type='thinking' for reasoning tokens and strings for final content.
        """
        headers = self._pow_headers("/api/v0/chat/completion")
        mid = self._msg_counters.get(session_id, 0) + 1
        self._msg_counters[session_id] = mid
        body = {
            "chat_session_id": session_id,
            "parent_message_id": mid - 1 if mid > 1 else None,
            "model_type": model_type,
            "prompt": prompt,
            "ref_file_ids": [],
            "stream": True,
            "thinking_enabled": thinking_enabled,
            "search_enabled": search_enabled,
            "preempt": False,
        }
        resp = self._client.post(
            f"{BASE_URL}/api/v0/chat/completion",
            json=body, headers=headers, stream=True,
        )
        try:
            kind = self._detect_waf_challenge(resp.status_code, resp.headers)
            if kind:
                # Drain so the connection can be reused.
                try:
                    body_text = resp.text
                except Exception:
                    body_text = ""
                raise WAFChallengeError(kind, resp.status_code, body_text)
            resp.raise_for_status()
            frag_type = None  # None, 'thinking', 'content'

            for line in resp.iter_lines():
                # curl_cffi yields bytes from iter_lines.
                if isinstance(line, (bytes, bytearray)):
                    try:
                        line = line.decode("utf-8")
                    except UnicodeDecodeError:
                        continue
                line = line.strip()
                if not line:
                    continue
                if not line.startswith("data: "):
                    continue

                data_str = line[6:]
                if not data_str:
                    continue
                try:
                    data = json.loads(data_str)
                except json.JSONDecodeError:
                    continue
                if not isinstance(data, dict):
                    continue

                p = data.get("p", "")
                o = data.get("o", "")
                v = data.get("v", "")

                # Initial response with fragments (expert mode)
                if isinstance(v, dict) and 'response' in v:
                    resp_data = v['response']
                    fragments = resp_data.get('fragments', [])
                    if fragments:
                        ft = fragments[0].get('type', '')
                        frag_type = 'thinking' if ft == 'THINK' else 'content'
                        fc = fragments[0].get('content', '')
                        if fc:
                            if frag_type == 'thinking':
                                yield {"__type": "thinking", "content": fc}
                            else:
                                yield fc
                    else:
                        frag_type = 'content'
                        content = resp_data.get('content', '')
                        if content:
                            yield content
                    continue

                # Fragment content append (expert mode)
                if p == "response/fragments/-1/content" and o == "APPEND":
                    if frag_type == 'thinking':
                        if v:
                            yield {"__type": "thinking", "content": v}
                    else:
                        if v:
                            yield v
                    continue

                # Fragment content without o (after fragment switch in batched responses)
                if p == "response/fragments/-1/content" and not o:
                    if frag_type == 'thinking':
                        if v:
                            yield {"__type": "thinking", "content": v}
                    else:
                        if v:
                            yield v
                    continue

                # Fragment switch (expert mode)
                if p == "response/fragments" and o == "APPEND":
                    if isinstance(v, list) and v:
                        new_type = v[0].get('type', '')
                        if new_type == 'RESPONSE':
                            frag_type = 'content'
                        elif new_type == 'THINK':
                            frag_type = 'thinking'
                    continue

                # Normal mode content
                if p == "response/content" and o == "APPEND":
                    yield v
                    continue

                # Plain token event
                if "v" in data and "p" not in data and "o" not in data:
                    token = data["v"]
                    if isinstance(token, str) and token:
                        if frag_type == 'thinking':
                            yield {"__type": "thinking", "content": token}
                        else:
                            yield token
                    continue

                # Status
                if p == "response/status":
                    yield {"__type": "status", "status": v}
                    continue
        finally:
            try:
                resp.close()
            except Exception:
                pass
