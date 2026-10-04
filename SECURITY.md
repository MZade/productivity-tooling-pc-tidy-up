# Security

PC TidyUp runs entirely on your own computer. It has no cloud service, no telemetry and no network access, apart from the browser talking to the local app on `127.0.0.1`. The local app's safeguards are described in [How it works](docs/how-it-works.md#the-local-app-and-its-security-model).

## Reporting a vulnerability

If you find a security problem, for example a way to make the local app act on files outside the current report, please **don't open a public issue**. Report it privately via GitHub's **Security → Report a vulnerability** on this repository. Please include steps to reproduce.

## Good practice

- Run PC TidyUp without administrator rights unless you need system folders.
- Treat the `reports\` folder as personal data: it contains file paths and your PC name.
- Review what a delete or archive will do before you confirm it.
