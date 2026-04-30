# Enterprise Test Fixtures

This directory holds enterprise PDF/DOCX samples used for regression testing.

## Files

| File | Description | Tags |
|------|-------------|------|
| `sap-long.pdf` | SAP类长篇多栏PDF，约50+页 | layout, slow |
| `cn-policy.pdf` | 中文政策/规程类PDF，含CID乱码页面 | fast |
| `manual.docx` | 大型操作手册DOCX，含章节标题和表格 | docx |

## Adding a new fixture

1. Drop the file here.
2. Run `python tools/regression_baseline.py --name <suite-name>` to generate a baseline.
3. Commit the baseline JSON to `var/regression/`.
4. Do **not** commit the fixture binary (see `.gitignore`).

## SHA256 checksums

Update this section after adding each fixture:

```
sap-long.pdf      = <sha256>
cn-policy.pdf     = <sha256>
manual.docx       = <sha256>
```
