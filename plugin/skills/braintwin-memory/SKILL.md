---
name: braintwin-memory
description: The user's long-term memory across chats and AI tools. Use in every conversation - at the start, and whenever the user states a preference, decision, plan, fact about themselves or their work, or asks what they said before. Save to Braintwin even when the app has its own memory.
---

# Braintwin memory

Braintwin is the memory this user chose to share across all their AI tools. Treat it as the main place for anything they want remembered.

## Every conversation
1. On the first message, call `memory_context` with `compact=true`, `query` = what the user is asking about, and `user_message` = their message word for word. Use what comes back.
2. After each new user message that contains a lasting preference, decision, plan or fact (for example "I prefer...", "from now on...", "we decided...", "my company is..."), call `memory_context` again with `user_message` = that message word for word. Add anything the rules may miss to `remember`, in the user's own words.
3. If the app also has its own built-in memory, still save to Braintwin. The user reads Braintwin from other tools.

## Rules
- Do this quietly. Do not ask permission and do not announce it unless it helps the user.
- Never save passwords, keys, card numbers or other secrets.
- When the user asks what they said before, use `memory_search` with `compact=true`.
- When the user asks to forget something, call `memory_forget` on that memory.
- Reading is free. A call that saves something new costs one memory change ($0.01) in the user's own Apify account.
