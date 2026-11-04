"""Minimal, configurable LLM provider layer for unattended CV generation.

Configuration comes only from the environment (.env), never from source code:

    LLM_PROVIDER=gemini            # currently the only implemented provider
    GEMINI_API_KEY=...             # Google AI Studio key (https://aistudio.google.com/apikey)
    LLM_MODEL=gemini-3.6-flash     # any Gemini model that supports generateContent
    LLM_TIMEOUT_SECONDS=120

Errors are classified so the caller can decide what to do:
    TemporaryLLMError     -> retry later with backoff (429, 5xx, timeouts, network)
    ConfigurationLLMError -> do not burn attempts; fix .env (missing/invalid key, unknown model)
    PermanentLLMError     -> this request will not succeed as-is (blocked, malformed reply)
"""
import json
import os

import httpx

GEMINI_BASE = 'https://generativelanguage.googleapis.com/v1beta'
DEFAULT_MODEL = 'gemini-3.6-flash'
TEMPORARY_STATUS = {408, 429, 500, 502, 503, 504}
CONFIG_STATUS = {401, 403, 404}


class LLMError(Exception):
    pass


class TemporaryLLMError(LLMError):
    def __init__(self, message, retry_after=None):
        super().__init__(message)
        self.retry_after = retry_after


class ConfigurationLLMError(LLMError):
    pass


class PermanentLLMError(LLMError):
    pass


def _retry_after(response):
    value = response.headers.get('retry-after', '')
    return float(value) if value.replace('.', '', 1).isdigit() else None


def _error_message(response):
    """Short provider error text. Never includes request content or the API key."""
    try:
        err = response.json().get('error', {})
        return f"HTTP {response.status_code} {err.get('status', '')}: {str(err.get('message', ''))[:300]}".strip()
    except (ValueError, AttributeError):
        return f'HTTP {response.status_code}'


class GeminiProvider:
    name = 'gemini'

    def __init__(self, api_key, model=DEFAULT_MODEL, timeout=120.0, transport=None):
        if not api_key:
            raise ConfigurationLLMError('GEMINI_API_KEY is not set in .env')
        self.model = model.removeprefix('models/')
        self._api_key = api_key
        self._timeout = timeout
        self._transport = transport  # injectable for tests

    def _client(self):
        return httpx.AsyncClient(timeout=self._timeout, transport=self._transport,
                                 headers={'x-goog-api-key': self._api_key})

    async def _request(self, method, url, **kwargs):
        try:
            async with self._client() as client:
                response = await client.request(method, url, **kwargs)
        except httpx.TimeoutException as exc:
            raise TemporaryLLMError(f'Gemini request timed out ({type(exc).__name__})') from None
        except httpx.TransportError as exc:
            raise TemporaryLLMError(f'Gemini network error ({type(exc).__name__})') from None
        if response.status_code in TEMPORARY_STATUS:
            raise TemporaryLLMError(_error_message(response), _retry_after(response))
        if response.status_code in CONFIG_STATUS:
            raise ConfigurationLLMError(_error_message(response))
        if response.status_code >= 400:
            raise PermanentLLMError(_error_message(response))
        try:
            return response.json()
        except ValueError:
            raise TemporaryLLMError('Gemini returned a non-JSON response') from None

    async def generate_json(self, system, prompt, temperature=0.2, max_output_tokens=32768):
        """Return the model's reply parsed as a JSON object."""
        body = {
            'systemInstruction': {'parts': [{'text': system}]},
            'contents': [{'role': 'user', 'parts': [{'text': prompt}]}],
            'generationConfig': {'temperature': temperature, 'maxOutputTokens': max_output_tokens,
                                 'responseMimeType': 'application/json'},
        }
        data = await self._request('POST', f'{GEMINI_BASE}/models/{self.model}:generateContent', json=body)
        block = (data.get('promptFeedback') or {}).get('blockReason')
        if block:
            raise PermanentLLMError(f'Gemini blocked the prompt: {block}')
        candidates = data.get('candidates') or []
        if not candidates:
            raise TemporaryLLMError('Gemini returned no candidates')
        candidate = candidates[0]
        finish = candidate.get('finishReason', '')
        text = ''.join(p.get('text', '') for p in (candidate.get('content') or {}).get('parts', [])
                       if not p.get('thought'))
        if finish == 'MAX_TOKENS':
            raise PermanentLLMError('Gemini reply was cut off (MAX_TOKENS)')
        if finish not in ('STOP', '', 'FINISH_REASON_UNSPECIFIED'):
            raise PermanentLLMError(f'Gemini stopped generating: {finish}')
        text = text.strip()
        if text.startswith('```'):
            text = text.split('\n', 1)[-1].rsplit('```', 1)[0]
        try:
            result = json.loads(text)
        except ValueError:
            raise PermanentLLMError('Gemini reply was not valid JSON') from None
        if not isinstance(result, dict):
            raise PermanentLLMError('Gemini reply was not a JSON object')
        return result

    async def describe_model(self):
        """models.get for the configured model (used by `--check`)."""
        return await self._request('GET', f'{GEMINI_BASE}/models/{self.model}')

    async def list_models(self):
        models, token = [], None
        while True:
            params = {'pageSize': 100, **({'pageToken': token} if token else {})}
            data = await self._request('GET', f'{GEMINI_BASE}/models', params=params)
            models += data.get('models', [])
            token = data.get('nextPageToken')
            if not token:
                return models


def provider_from_env(transport=None):
    name = os.getenv('LLM_PROVIDER', 'gemini').strip().lower()
    if name != 'gemini':
        raise ConfigurationLLMError(f'Unsupported LLM_PROVIDER {name!r} (supported: gemini)')
    return GeminiProvider(os.getenv('GEMINI_API_KEY', '').strip(),
                          os.getenv('LLM_MODEL', DEFAULT_MODEL).strip() or DEFAULT_MODEL,
                          float(os.getenv('LLM_TIMEOUT_SECONDS', '120')), transport=transport)
