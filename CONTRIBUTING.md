# Contributing

Explain the concrete problem and desired behavior. Discuss substantial features
before building large changes. Keep pull requests focused.

~~~bash
pip install -r server/requirements-dev.txt
python server/tests/logic_test.py
python -m pytest
cd android
./gradlew assembleDebug assembleRelease lintDebug
~~~

Use synthetic data, never household databases, secrets, private endpoints, signing
material, or child activity. HTTP tests use isolated databases with external APIs
disabled. Add meaningful behavior, permission, and migration regressions.
Keep docs accurate about playback and parental controls. Builds do not replace
real-device Android/TV playback and remote checks.

PRs should explain behavior, purpose, validation, and limitations. Security reports
follow [SECURITY.md](SECURITY.md). Contributions use MIT; include attribution and
licenses for added third-party code/assets.
