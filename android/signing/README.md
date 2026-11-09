Signing key for the sideloaded AI Amalgam Android app. Android only accepts an update if it is
signed with the same key as the installed app, so this key must stay the same.

It is in this public repo for convenience. Anyone could sign an app with it, but your phone would only
install such an app if you chose to install it yourself. To keep it private instead, add repository
secrets AMALGAM_KEYSTORE_B64 (base64 of a .p12) and AMALGAM_KEYSTORE_PASSWORD; the build prefers them.
(Switching keys means uninstalling the app once.)
