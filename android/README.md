# The Daily Telly for Android and TV

Kotlin/Compose, Android API 24+, namespace and application ID com.dailytelly.app.
This is the Daily Telly package identity. Apps installed under a different package
need a fresh installation and pairing. Updates within this package must use the
same signing key.

## First launch

1. The TV shows a fresh eight-character code and https://thedailytelly.com/activate.
2. An adult opens that address on a phone/browser and signs in. The platform verifies the adult email with a browser-bound sign-in link.
3. They enter the code and explicitly confirm it is visible on their own screen.
4. The app polls with a separate high-entropy secret and receives its own viewing token.
5. The parent can revoke that TV in the account's Linked devices section.

No passwords or email addresses are entered on the TV. Pairing expires in 10 minutes.
On the first platform upgrade, the old shared token is cleared and the saved
server URL is replaced by https://thedailytelly.com. The TV must pair afresh.
If credential delivery is interrupted, request a new code. Long-pressing the title
opens pairing/settings. Linking to another account clears this app's local token;
revoke its old credential in the previous account too.

## Build

JDK 17+, SDK 35, Build Tools 36.0.0, Gradle 9.6.1 / AGP 9.2.1:

~~~bash
./gradlew assembleDebug assembleRelease lintDebug
# Emulator development:
./gradlew assembleDebug -PplatformUrl=http://10.0.2.2:18400
~~~

Release builds require HTTPS and are unsigned until you configure signing.
Debug builds allow local development HTTP. Backup is disabled; tokens are private
app preferences. No analytics SDK, advertising SDK, or fingerprinting is added.

Playback now uses the standard unobscured YouTube controls, privacy-enhanced embeds,
and no codec capability interception. YouTube can still process data and show ads.
External player links may open the browser; this is not kiosk software.

Actual phone/TV playback and remote-control tests are required before a broad release.
