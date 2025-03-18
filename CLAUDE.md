# PyWAI Development Guide

## Creating a New Feature

### 1. Development Process

1. **Understand the codebase structure**:
   - `pywa/`: Core WhatsApp API library
   - `pywaai/`: AI integration for WhatsApp
   - `examples/`: Example applications
   - `tests/`: Test suite

2. **Make code changes**:
   - Implement new feature in the appropriate module
   - Follow existing code style and patterns
   - Add appropriate docstrings and type hints

3. **Version and changelog updates**:
   - Bump version in `pywaai/__init__.py`
   - Add entry to `CHANGELOG.md` with the new version and description

4. **Testing**:
   - Add unit tests in the `tests/` directory
   - Run tests with `python -m pytest`

5. **Commit and release**:
   - Commit changes with descriptive message
   - Push to the repository
   - Release package (handled by maintainers)

### 2. Common Commands

```bash
# Run tests
python -m pytest

# Run specific test
python -m pytest tests/test_conversation_db.py

# Lint code
# [Add linting commands if available]

# Check types
# [Add type checking commands if available]
```

### 3. Code Style Guidelines

- Follow PEP 8 for Python code style
- Use type hints for all functions and methods
- Document all public APIs with docstrings
- Keep code modular and maintainable

### 4. Template Language Support

For adding new language templates:
1. Add the language to `pywa/types/template.py` in the `Language` enum
2. Follow the naming convention (LANGUAGE_COUNTRY format)
3. Use the correct locale code as the value

### 5. Package Release Process

For maintainers:
1. Verify all changes are committed and pushed
2. Ensure version is updated in `pywaai/__init__.py`
3. Ensure changelog is updated with new version and changes
4. Create a new release on the package repository