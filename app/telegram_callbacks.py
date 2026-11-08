"""One entry point for Telegram button presses (called by the polling loop in app/main.py).

- `wf:` buttons: approval decisions (workflow.decide: Prepare / Approve & Submit / Reject) - unchanged.
- `m?:` buttons: the mobile manual-application workflow (app/applications/manual.py) - only records what YOU did;
  never queues, submits or changes APPLY_MODE.
Every press is authorized against TELEGRAM_CHAT_ID and TELEGRAM_ALLOWED_USER_IDS first.
"""
from . import workflow
from .applications import manual
from .core import log


async def handle(client, token, cb, *, allowed_chat, allowed_users):
    api = f'https://api.telegram.org/bot{token}'
    message = cb.get('message') or {}
    chat_id = (message.get('chat') or {}).get('id', '')
    user_id = (cb.get('from') or {}).get('id', '')
    data = cb.get('data') or ''
    if manual.is_manual_callback(data):
        if not workflow.telegram_authorized(chat_id, user_id, allowed_chat, allowed_users):
            log.warning('Rejected manual-workflow callback from unauthorized chat/user')
            result = {'answer': 'Unauthorized', 'remove': False}
        else:
            result = manual.handle_callback(data, user_id=user_id)
    else:
        answer, remove = workflow.handle_telegram_callback(data, chat_id=chat_id, user_id=user_id,
                                                            allowed_chat=allowed_chat, allowed_users=allowed_users)
        result = {'answer': answer, 'remove': remove}
    await client.post(f'{api}/answerCallbackQuery', json={'callback_query_id': cb['id'], 'text': result['answer'][:190], 'show_alert': True})
    if result.get('remove') and message.get('message_id'):        # no second press on a decided/stale message
        await client.post(f'{api}/editMessageReplyMarkup', json={'chat_id': message['chat']['id'], 'message_id': message['message_id'],
                                                                'reply_markup': {'inline_keyboard': []}})
    if result.get('followup'):
        text, markup = result['followup']
        await client.post(f'{api}/sendMessage', json={'chat_id': allowed_chat, 'text': text, 'reply_markup': markup})
    if result.get('edit'):
        message_id, text, markup = result['edit']
        await client.post(f'{api}/editMessageText', json={'chat_id': allowed_chat, 'message_id': message_id, 'text': text[:4000],
                                                         'reply_markup': markup})
    return result
