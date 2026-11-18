# Licensing

NL2Prompt is **dual-licensed**. Pick the one that fits your use case.

---

## 1. Open-source license: GNU AGPL-3.0 (default)

The project is released under the **GNU Affero General Public License v3.0** — see [LICENSE](LICENSE) for the full text.

You may use, modify and distribute it **for free** under AGPL-3.0, provided that:

- **Personal / internal use** — free, no obligations beyond keeping the copyright notice.
- **Non-commercial distribution** — free, but the complete corresponding source of your modified version must also be released under AGPL-3.0.
- **Commercial use that is open-sourced** — free, same AGPL-3.0 obligations.
- **Commercial use that is **closed-source** (shipping a product, or operating a network service, without releasing the full source)** — **requires a commercial license**.
- **Modification** — must be declared: retain the copyright notice, mark the changed files, and state the changes (AGPL-3.0 §5).

Because AGPL-3.0 §13 covers **network use**, running a modified version as a hosted service (SaaS) also triggers the source-release obligation. If you do not want to open-source your service, buy a commercial license instead.

> AGPL-3.0 is a strong copyleft license. In short: **use it freely, but if you build on it and give it to others (or serve it over a network), your version must be AGPL too.**

---

## 2. Commercial license (for closed-source use)

If you want to use NL2Prompt in a **closed-source product** or a **proprietary hosted service**, a commercial license is required. It removes the AGPL-3.0 copyleft obligations and grants you:

- the right to integrate the code into proprietary software without disclosing your source;
- the right to distribute and/or host modified versions commercially;
- (optionally) priority support and custom development.

See [LICENSE-COMMERCIAL.md](LICENSE-COMMERCIAL.md) for the license template and how to request one.

**Contact:** `gqli_vc@163.com`

---

## 3. Which one do I need?

| Your situation | License needed |
|---|---|
| Personal use, learning, experiments | **AGPL-3.0** (free) |
| Studio / company internal tool, not distributed | **AGPL-3.0** (free) |
| Open-source project that ships NL2Prompt | **AGPL-3.0** (free) |
| Closed-source product that bundles NL2Prompt | **Commercial** |
| Paid SaaS built on a modified NL2Prompt | **Commercial** |
| Consulting / client delivery where the client receives source | **AGPL-3.0** or **Commercial** (your choice) |

> Not sure? Use AGPL-3.0 first. If you later need to go closed-source, you can buy a commercial license — no retroactive penalty as long as the AGPL-obligated period was properly honoured.

---

## 4. Third-party components

NL2Prompt **does not contain code copied from other ComfyUI plugins**. It was verified against the two projects whose UI patterns inspired it:

| Project | License | Code reuse |
|---|---|---|
| [nregret/Comfyui-Anima-Tools](https://github.com/nregret/Comfyui-Anima-Tools) | MIT (declared in `pyproject.toml`; repo has no LICENSE file) | none (n-gram check: 0 blocks ≥4 lines) |
| [n0va39/ComfyUI-EasyUseAnima](https://github.com/n0va39/ComfyUI-EasyUseAnima) | GPL-3.0 | **none** (n-gram check: 0 blocks ≥3 lines) |

Only *design patterns* (hidden holder widget, standalone settings dialog) were borrowed — patterns are not copyrightable.

Runtime dependencies are all permissive and Apache-2.0-compatible:

| Dependency | License |
|---|---|
| transformers, accelerate, safetensors, tokenizers, huggingface_hub, aiohttp | Apache-2.0 |
| PyTorch | BSD-3-Clause |
| NumPy | BSD-3-Clause |

**ComfyUI itself is GPL-3.0.** NL2Prompt is an independent plugin: it contains no ComfyUI source code and interacts only through the public registration API (`NODE_CLASS_MAPPINGS`, `app.registerExtension`). AGPL-3.0 is compatible with this arrangement and is the same family as ComfyUI's own license.

---

## 5. Copyright

```
Copyright (C) 2026 GQLI-VC
```

The `LICENSE` file itself is kept byte-for-byte identical to the official AGPL-3.0 text (as GNU recommends); the copyright notice lives in the source-file headers and in this document instead.

---

## 6. FAQ

**Q: Can I use it in my company for internal workflows?**
Yes, AGPL-3.0, free. Internal use is not distribution.

**Q: Can I sell a product built on it?**
Only if the product is itself AGPL-3.0 and you release its source. Otherwise you need a commercial license.

**Q: I modified it for myself and never shared it — do I have to publish?**
No. AGPL obligations trigger on distribution or network service, not on private modification.

**Q: Can I put it on a paid ComfyUI cloud and let users run it?**
That is a network service. Either open-source your whole service under AGPL-3.0, or buy a commercial license.

**Q: Does the commercial license let me relicense it as MIT?**
No. A commercial license grants you rights to use NL2Prompt; it does not transfer copyright.
