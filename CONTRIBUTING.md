# Contributing to Amazon HealthSignals

Thank you for your interest in contributing! This project is in its early Blueprint phase and welcomes contributions from epidemiologists, public health practitioners, and cloud engineers.

## How to Contribute

### Reporting Issues
- Use GitHub Issues for bugs, feature requests, or questions
- Include your deployment region and CDK version for infrastructure issues
- For data source issues, include the API response and timestamp

### Pull Requests
1. Fork the repository
2. Create a feature branch (`git checkout -b feature/my-improvement`)
3. Make your changes with clear commit messages
4. Add/update tests as appropriate
5. Ensure `pytest tests/unit/` passes
6. Submit a PR with a clear description of the change

### Areas Where Help Is Needed
- **Epidemiologists:** Validate threshold parameters, suggest additional sentinel signals
- **Public Health Practitioners:** Review communication templates, suggest alert formats
- **Cloud Engineers:** Improve CDK constructs, add observability, optimize costs
- **Data Engineers:** Add new data source integrations (state-specific feeds)

## Development Setup

```bash
# Clone and setup
git clone https://github.com/goginea/aws-healthsignals.git
cd aws-healthsignals/cdk
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install -r requirements-dev.txt  # includes pytest, black, mypy

# Run tests
pytest tests/unit/ -v

# Format code
black lambdas/ tests/
```

## Code Style
- Python: Black formatter, type hints encouraged
- CDK: One construct per logical component
- Lambdas: Keep handlers thin — business logic in separate modules

## Commit Messages
Use conventional commits:
- `feat:` new feature
- `fix:` bug fix
- `docs:` documentation only
- `test:` adding/updating tests
- `infra:` CDK/infrastructure changes

## Code of Conduct
Be respectful, constructive, and remember this tool is intended to help underserved rural communities. Keep that mission in focus.

## License
By contributing, you agree that your contributions will be licensed under the Apache 2.0 License.
