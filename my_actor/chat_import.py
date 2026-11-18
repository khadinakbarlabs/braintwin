"""Read explicitly attributed chat exports, following the selected ChatGPT branch."""
import hashlib
from .capture import extract_candidates


def _messages(chat):
    mapping = chat.get('mapping')
    if isinstance(mapping, dict):
        path = []
        node_id = chat.get('current_node')
        seen = set()
        while isinstance(node_id, str) and node_id not in seen:
            node = mapping.get(node_id)
            if not isinstance(node, dict):
                break
            seen.add(node_id)
            if isinstance(node.get('message'), dict):
                path.append((node_id, node['message']))
            node_id = node.get('parent')
        return reversed(path)
    messages = chat.get('chat_messages', chat.get('messages', []))
    return ((str(m.get('uuid') or m.get('id') or i), m) for i, m in enumerate(messages) if isinstance(m, dict))


def extract_chat_records(payload):
    chats = payload if isinstance(payload, list) else [payload]
    if not chats or not all(isinstance(c, dict) and (isinstance(c.get('mapping'), dict)
                            or isinstance(c.get('chat_messages'), list) or isinstance(c.get('messages'), list)) for c in chats):
        return None
    rows = []
    for chat in chats:
        conversation = str(chat.get('id') or chat.get('uuid') or chat.get('title') or 'chat')
        for message_id, message in _messages(chat):
            author = message.get('author')
            role = author.get('role') if isinstance(author, dict) else message.get('sender') or message.get('role')
            if role not in ('user', 'human'):
                continue
            content = message.get('text') or message.get('content')
            if isinstance(content, dict):
                content = content.get('parts')
            if isinstance(content, list):
                content = '\n'.join(p if isinstance(p, str) else p.get('text', '') for p in content
                                    if isinstance(p, str) or (isinstance(p, dict) and p.get('type') == 'text' and isinstance(p.get('text'), str)))
            if not isinstance(content, str):
                continue
            for fact in extract_candidates(content):
                identifier = hashlib.sha256(f"{conversation}|{message_id}|{fact['content']}".encode()).hexdigest()
                rows.append({**fact, 'id': identifier, 'citations': [{'type': 'chat', 'conversation': conversation,
                                                                      'message': message_id}]})
    return rows
