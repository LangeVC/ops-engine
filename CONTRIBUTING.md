# Contributing to ops-engine

First off, thanks for taking the time to contribute!

`ops-engine` is an open-source framework designed to abstract away the complexity of building rate-limited, generic webhook queues for GitHub and Forgejo.

## Architectural Philosophy (The "Engine" vs "Layover" Model)

Before submitting code, please understand our core philosophy:
1. **No Business Logic:** `ops-engine` MUST NOT contain organization-specific logic (e.g., "if repo is X, assign user Y").
2. **Config-Driven:** All feature flags, thresholds (like `days_until_stale`), and target repositories must be configurable via the Pydantic models in `config_loader.py`.
3. **Abstract Adapters:** Direct HTTP calls should be abstracted behind `ForgeAdapter` implementations.

## Local Development

1. Fork the repo and create your branch from `master`.
2. Install dependencies: `pip install -e .`
3. We use `hatch` for builds. 
4. Ensure your code is properly formatted (PEP 8 standard).

## Pull Requests

1. **Keep it small**: Focus on a single feature or bug fix.
2. **Test your code**: Ensure the webhook handlers and async queues don't block the event loop.
3. **Update documentation**: If you add a config option, update the `README.md`.

## License

By contributing, you agree that your contributions will be licensed under its Apache 2.0 License.
