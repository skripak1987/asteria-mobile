[app]
title = Asteria Mobile
package.name = asteriamobile
package.domain = org.asteria
source.dir = .
source.include_exts = py,png,jpg,kv,atlas,json
version = 1.0
# ccxt потрібен лише для LIVE-торгівлі на BingX. Якщо збірка з ccxt падає, прибери з цього рядка
# ",ccxt,setuptools,typing_extensions" — застосунок збереться і працюватиме в режимі PAPER.
requirements = python3,kivy==2.3.0,requests,urllib3,chardet,idna,certifi,charset-normalizer,setuptools,typing_extensions,ccxt
orientation = portrait
fullscreen = 0
icon.filename = %(source.dir)s/icon.png
android.permissions = INTERNET,ACCESS_NETWORK_STATE,WAKE_LOCK,FOREGROUND_SERVICE,REQUEST_IGNORE_BATTERY_OPTIMIZATIONS
android.api = 33
android.minapi = 24
android.archs = arm64-v8a
android.accept_sdk_license = True
android.wakelock = True

[buildozer]
log_level = 2
warn_on_root = 0
