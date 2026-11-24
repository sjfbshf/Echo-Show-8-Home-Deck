#!/bin/sh
# Builds out/open-sesame.apk with Google's command-line build tools (no Gradle or Android Studio needed).
# Needs Git Bash (Windows), a JDK, Python, build-tools 36 and the android-28 platform from Google.
#   sh echo-app/build.sh
#   adb install -r echo-app/out/open-sesame.apk
set -e
cd "$(dirname "$0")"
# Where Google's build tools, the Android 9 platform and your signing key live (override with env vars)
BT=${BUILD_TOOLS:-/c/echo/android/bt/android-16}
JAR=${ANDROID_JAR:-/c/echo/android/pf/android-9/android.jar}
KS=${KEYSTORE:-/c/echo/android/debug.keystore}
KEYTOOL=${KEYTOOL:-"/c/Program Files/Java/jdk-22/bin/keytool.exe"}
w() { cygpath -w "$1"; }

rm -rf out && mkdir -p out/classes
"$BT/aapt2.exe" compile --dir res -o out/res.zip
"$BT/aapt2.exe" link -I "$(w $JAR)" --manifest AndroidManifest.xml -o out/base.apk out/res.zip
javac --release 8 -nowarn -Xlint:-options -classpath "$(w $JAR)" -d out/classes src/com/homedeck/opensesame/*.java
(cd out && python -c "import shutil; shutil.make_archive('classes', 'zip', 'classes')" && mv classes.zip classes.jar)
cmd //c "$(w $BT/d8.bat)" --min-api 28 --lib "$(w $JAR)" --output "$(w out)" "$(w out/classes.jar)"
(cd out && python -c "import zipfile; z = zipfile.ZipFile('base.apk', 'a'); z.write('classes.dex'); z.close()")
"$BT/zipalign.exe" -f 4 out/base.apk out/aligned.apk
[ -f "$KS" ] || "$KEYTOOL" -genkeypair -keystore "$(w $KS)" -storepass android -keypass android -alias deck \
  -keyalg RSA -keysize 2048 -validity 10000 -dname "CN=Home Deck" >/dev/null 2>&1
cmd //c "$(w $BT/apksigner.bat)" sign --ks "$(w $KS)" --ks-pass pass:android --out "$(w out/open-sesame.apk)" "$(w out/aligned.apk)"
echo "built echo-app/out/open-sesame.apk"
