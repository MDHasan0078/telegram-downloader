package com.tgdownloader.telegram_downloader

import com.chaquo.python.android.PyApplication

/**
 * Chaquopy starts the Python runtime from the Application class, so the app
 * has to have one.
 *
 * The manifest names `.TgApplication`, and without this class the manifest
 * merger fails with "Class referenced in the manifest was not found in
 * project" -- and, if it somehow got past that, the Python side would never
 * start, so the app would show an empty window.
 */
class TgApplication : PyApplication()
