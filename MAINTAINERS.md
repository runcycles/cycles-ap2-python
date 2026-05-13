# Maintainers

This package is part of the [runcycles](https://github.com/runcycles) ecosystem.

- Issues: https://github.com/runcycles/cycles-ap2-python/issues
- Sibling: https://github.com/runcycles/cycles-client-python
- Upstream protocol being wrapped: https://github.com/google-agentic-commerce/AP2

## Release process

1. Bump `version` in `pyproject.toml`.
2. Update `CHANGELOG.md` and `AUDIT.md`.
3. Tag: `git tag v0.1.0 && git push --tags`.
4. CI builds and publishes to PyPI.
