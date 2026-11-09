# forecast

Tiny helpers for turning temperatures into words.

```python
from forecast import describe
describe(22)  # "warm"
```

## Development

Dependencies are managed with [uv](https://docs.astral.sh/uv/) and live in the
project's `.venv` (Python 3.11+). `uv sync --extra dev` recreates it.
