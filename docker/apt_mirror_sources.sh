#!/bin/sh
set -eu

sources_file="${APT_SOURCES_FILE:-/etc/apt/sources.list.d/debian.sources}"

escape_sed_replacement() {
    printf '%s' "$1" | sed 's/[&|\\]/\\&/g'
}

if [ -n "${APT_MIRROR_URL:-}" ]; then
    apt_mirror_url="$(escape_sed_replacement "$APT_MIRROR_URL")"
    sed -i \
        "s|^URIs: http://deb.debian.org/debian$|URIs: ${apt_mirror_url}|" \
        "$sources_file"
fi

if [ -n "${APT_SECURITY_MIRROR_URL:-}" ]; then
    apt_security_mirror_url="$APT_SECURITY_MIRROR_URL"
elif [ -n "${APT_MIRROR_URL:-}" ]; then
    apt_security_mirror_url="$APT_MIRROR_URL"
else
    apt_security_mirror_url=""
fi

if [ -n "$apt_security_mirror_url" ]; then
    apt_security_mirror_url="$(escape_sed_replacement "$apt_security_mirror_url")"
    sed -i \
        "s|^URIs: http://deb.debian.org/debian-security$|URIs: ${apt_security_mirror_url}|" \
        "$sources_file"
fi
