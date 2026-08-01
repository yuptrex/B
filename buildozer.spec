[app]
title = LAN Chat
package.name = lanchat
package.domain = org.friendchat

source.dir = .
source.include_exts = py,png,jpg,kv,atlas

version = 1.0

# Kivy + pure-python deps only - sqlite3 and socket/threading/json
# are all part of the Python standard library, no extra packages needed.
requirements = python3,kivy==2.3.0

orientation = portrait
fullscreen = 0

# --- Android permissions ---
# INTERNET: required even for local-only sockets (Android treats any
#           TCP/UDP socket use as needing this permission)
# ACCESS_WIFI_STATE / ACCESS_NETWORK_STATE: to read local IP + hotspot info
# CHANGE_WIFI_MULTICAST_STATE: needed on some devices for UDP broadcast
#           to work reliably over the hotspot's WiFi interface
android.permissions = INTERNET,ACCESS_WIFI_STATE,ACCESS_NETWORK_STATE,CHANGE_WIFI_MULTICAST_STATE

android.api = 33
android.minapi = 21
android.ndk = 25b
android.archs = arm64-v8a, armeabi-v7a

android.allow_backup = True

[buildozer]
log_level = 2
warn_on_root = 1
