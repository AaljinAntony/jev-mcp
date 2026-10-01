# Why MIT

`jev-engine` is MIT licensed. This page keeps the reasoning, in case anyone
needs to revisit it or reuse the code under different terms.

The choice comes down to adoption. This is a small developer tool that people
install into their agent harness to do one job, so the licence is chosen to
maximise the number of people who can use it:

- **MIT is the most permissive option**, so corporate MCP clients, commercial
  tools and closed-source teams can adopt it without a legal review cycle.
- **It matches the two upstream projects** the code was derived from, so there
  is no relicensing friction or attribution conflict.
- **It needs no CLA.** Contributors keep their copyright.

## What else was available

| Licence | Why not |
|---|---|
| **Apache-2.0** | Equally permissive, and adds an explicit patent grant plus a patent-retaliation clause. The stronger patent terms are a real benefit, but it is a ~11 kB file and most contributors read less of it than of MIT's ~1 kB. Reasonable if corporate adoption of your forks is the main concern. |
| **MPL-2.0** | File-level copyleft: forks of these Python files must stay open, but new files in a fork may be proprietary. Good if you want improvements to flow back without demanding a fully open fork. |
| **GPL-3.0 / AGPL-3.0** | Strong copyleft. AGPL would also stop someone running your server as a hosted service, which is the only way to prevent a cloud clone. It is also why AGPL scares off exactly the corporate and contributor adoption a small tool needs. Pick this only if protecting a hosted offering matters more than adoption. |

MIT, Apache-2.0, MPL-2.0 and GPL-3.0 are all OSI-approved, and MIT upstream
code can be redistributed under any of them with its notice retained. So this is
a genuine choice rather than a constraint.

## Notices

Parts of this project were derived from other MIT-licensed projects. Those
notices, with full license texts, are collected in
[`THIRD_PARTY_NOTICES.md`](../THIRD_PARTY_NOTICES.md).