📝 **Changelog**
---------------

> NOTE: pywai follows the [semver](https://semver.org/) versioning standard.

### 0.0.18 (2025-03-17)

- Add SPANISH_PER to the list of supported languages in template.py

### 0.0.17 (2025-03-08)

- Add tool_call_id to tool responses (fix)

### 0.0.13 (2025-03-08)

- Change execute_tools to handle both coroutines and non-coroutines

### 0.0.12 (2025-03-07)

- Add `delete_conversation` method to ConversationManager

### 0.0.11 (2024-12-31)

- Change env vars names for auth0

### 0.0.10 (2024-12-31)

- Add Auth0 M2M authentication in LocalOrRemoteConversation for remote conversation API calls

### 0.0.9 (2024-12-18)

- Use conversation manager in ai_utils

### 0.0.7 (2024-11-30)

- Add get_all_phone_numbers to ConversationDB

### 0.0.6 (2024-11-25)

- Replace ulid import

### 0.0.5 (2024-11-24)

- Add ConversationManager
- Change to SQLAlchemy for ORM
- Add test_conversation_db

### 0.0.4 (2024-11-18)

- Move init_db off asyncio.run

### 0.0.3 (2024-11-12) **Latest**

- Add message_text and user_name to send_message

### 0.0.2 (2024-11-12)

- Add requirements
- Rename to pywaai
- Fixes to conversation_db
- Add loguru and logfire optional dependencies


#### 0.0.1 (2024-11-11)

- Initial release
