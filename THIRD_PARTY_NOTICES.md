# Third-party notices

`jev-engine` is MIT licensed (see [LICENSE](LICENSE)). Parts of it were derived
from other MIT-licensed projects, and the MIT license requires the copyright
notice and permission notice to travel with the material. This file collects
those notices.

Nothing here changes the license of this repository: every component below is
MIT, which is compatible with this repository's MIT license.

---

## burnigtm/jev-mcp

- **Source:** https://github.com/burnigtm/jev-mcp
- **License:** MIT
- **Copyright:** Copyright (c) 2026 jev-mcp contributors

The Python modules in this repository were ported from this project's
TypeScript implementation. Each ported module names its origin in its module
docstring:

| This repository | Ported from |
|---|---|
| `config.py` | `src/config.ts` |
| `jev_errors.py` | `src/errors.ts` |
| `jev_validation.py` | `src/responses.ts` |
| `limits.py` | `src/limits.ts` |
| `mock.py` | `src/mock.ts` |
| `policy.py` | `src/policy.ts` |
| `jev_engine.py` | `src/typesafe.ts` (SDK retry-policy defaults) |

```
MIT License

Copyright (c) 2026 jev-mcp contributors

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

---

## jkudish/jev-mcp

- **Source:** https://github.com/jkudish/jev-mcp
- **License:** MIT
- **Copyright:** Copyright (c) 2026 Joey Kudish

`policy.py` draws its confidence and action arithmetic from this project's
`src/lib.ts` as well as `burnigtm/jev-mcp`'s `src/policy.ts`; the module
docstring names both.

```
MIT License

Copyright (c) 2026 Joey Kudish

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

---

## TypeSafe AI — `typesafe-ai` skill

- **Source:** `typesafe-ai/skills` (as recorded in `skills-lock.json`)
- **License:** MIT
- **Copyright:** Copyright (c) 2026 TypeSafe AI
- **Path in this repository:** `.agents/skills/typesafe-ai/`

Vendored as a reference for how TypeSafe describes its own models. Its full MIT
text is retained verbatim alongside it at
[`.agents/skills/typesafe-ai/LICENSE`](.agents/skills/typesafe-ai/LICENSE).
Tracked by hash in [`skills-lock.json`](skills-lock.json).

```
MIT License

Copyright (c) 2026 TypeSafe AI

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

---

## Runtime dependencies

This repository does not vendor its runtime dependencies. `requirements.txt`
pins them by exact version and each remains under its own license:

- [`typesafe-sdk`](https://pypi.org/project/typesafe-sdk/) — the Jev decision engine
- [`mcp`](https://pypi.org/project/mcp/) — the Model Context Protocol SDK (Python)
- [`pydantic`](https://pypi.org/project/pydantic/), [`httpx`](https://pypi.org/project/httpx/), [`python-dotenv`](https://pypi.org/project/python-dotenv/), [`tenacity`](https://pypi.org/project/tenacity/)
- [`pywin32`](https://pypi.org/project/pywin32/) — Windows only

The vendored OpenCode plugin (`config/jev-plugin.example.js`) has no runtime
dependencies: it is hand-rolled against the MCP wire format because no MCP SDK is
available in the OpenCode plugin sandbox.

If you fork this repository, carry this file forward with your own copyright
notice in `LICENSE`.