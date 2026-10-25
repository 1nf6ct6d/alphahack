"""Клиент к LLM по OpenAI-совместимому chat/completions (ПРЕДПОЛОЖЕНИЕ: формат API организаторов в ТЗ не описан).
Только стандартная библиотека. Ключ берётся из переменной окружения и никогда не печатается и не сохраняется."""
import json
import os
import time
import urllib.error
import urllib.request

ALLOWED_MODELS = ["Gemma 4 26B A4B", "GLM-5.3-Flash", "Qwen3.6 35B A3B", "DeepSeek-V4.1-Flash",
                  "Qwen3-VL-30B-A3B-Instruct", "Kimi-K2.6"]
MODES = ("json_schema", "json_schema_basic", "json_object", "none")


def scrub(text, secrets):
    out = str(text)
    for s in secrets:
        if s:
            out = out.replace(s, "[REDACTED]")
    return out


def scrub_obj(o, secrets):
    return json.loads(scrub(json.dumps(o, ensure_ascii=False), secrets)) if secrets else o


def response_format(mode, full_schema, basic_schema):
    if mode == "json_schema":
        return {"type": "json_schema", "json_schema": {"name": "bce_hypotheses", "schema": full_schema}}
    if mode == "json_schema_basic":
        return {"type": "json_schema", "json_schema": {"name": "bce_hypotheses", "schema": basic_schema}}
    if mode == "json_object":
        return {"type": "json_object"}
    if mode == "none":
        return None
    raise ValueError(f"Неизвестный режим structured output: {mode}")


class LlmClient:
    def __init__(self, base_url, api_key, timeout=180):
        self.url = base_url.rstrip("/") + "/chat/completions"
        self._key = api_key
        self.timeout = timeout

    def chat(self, model_id, system, user, temperature, max_tokens, resp_format):
        body = {"model": model_id, "temperature": temperature, "max_tokens": max_tokens,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
        if resp_format is not None:
            body["response_format"] = resp_format
        req = urllib.request.Request(self.url, data=json.dumps(body).encode("utf-8"), method="POST",
                                     headers={"Content-Type": "application/json", "Authorization": f"Bearer {self._key}"})
        t0 = time.time()
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                data = json.loads(r.read().decode("utf-8"))
            status = 200
        except urllib.error.HTTPError as e:
            return {"ok": False, "status": e.code, "error": scrub(e.read().decode("utf-8", "replace")[:1000], [self._key]),
                    "latency_s": round(time.time() - t0, 2)}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "status": None, "error": scrub(f"{type(e).__name__}: {e}", [self._key]),
                    "latency_s": round(time.time() - t0, 2)}
        choice = (data.get("choices") or [{}])[0]
        usage = data.get("usage") or {}
        return {"ok": True, "status": status, "text": (choice.get("message") or {}).get("content") or "",
                "finish_reason": choice.get("finish_reason"),
                "usage": {"input_tokens": usage.get("prompt_tokens"), "output_tokens": usage.get("completion_tokens")},
                "latency_s": round(time.time() - t0, 2)}


def get_api_key(env_name):
    return os.environ.get(env_name) or None
