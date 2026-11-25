# Collector Parts Finder: manuals reader

Finds freely posted factory parts catalogues and manuals on the Internet Archive and reads their
part-number lines into the Collector Parts Finder manuals library.

- `archive_manuals.py` finds manuals (no AI).
- `manual_reader.py` reads part lines: a free pattern reader first, Google Gemini for messy pages,
  Claude Haiku as a capped backup. Every line is checked against its page before it is saved.

All keys are GitHub secrets. Workflows run only on a schedule or by hand, never on pull requests.
