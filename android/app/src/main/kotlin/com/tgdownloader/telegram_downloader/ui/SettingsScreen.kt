package com.tgdownloader.telegram_downloader.ui

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.Row
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import com.tgdownloader.telegram_downloader.DependencyRow
import com.tgdownloader.telegram_downloader.Space
import com.tgdownloader.telegram_downloader.TgViewModel
import com.tgdownloader.telegram_downloader.Type
import com.tgdownloader.telegram_downloader.tgColors

/**
 * The Settings destination: dependencies, about, and the danger zone.
 *
 * Dependencies is a SECTION here, not a tab of its own: configuration and
 * environment belong together, and the download folder is not here either
 * -- it is changed from the bar along the bottom of the screen, so there is
 * one place to change one value.
 */
@Composable
fun SettingsScreen(vm: TgViewModel, onInstallUpdate: () -> Unit = {}) {
    val c = tgColors()
    var showReset by remember { mutableStateOf(false) }
    var phrase by remember { mutableStateOf("") }

    TgPage {
        // -- Dependencies
        //
        // This is the ANDROID list, built by the engine's bridge: ffmpeg
        // and cryptg are genuinely missing here and the app says so. The
        // flet row is in the list too, but it is marked as not applying
        // to this platform -- the desktop app really does ship flet, and
        // this one is Kotlin/Compose and never imports it.
        TgSectionLabel("Dependencies")
        if (vm.dependencies.isEmpty()) {
            Text(
                text = "Checking…",
                color = c.muted,
                fontSize = Type.meta,
            )
        }
        for (dep in vm.dependencies) {
            // A dependency that does not apply to this platform is not
            // shown: there is nothing the user can do about it here, and
            // a red MISSING badge for it would be a false alarm about
            // something the app cannot use and cannot install.
            if (!dep.applicable) continue
            DependencyLine(dep)
        }
        TgButton(
            text = "Re-check",
            kind = TgButtonKind.Secondary,
            onClick = vm::refreshDependencies,
            busy = vm.depsBusy,
        )

        // -- About
        TgSectionLabel("About")
        KeyValue("Version", "v${vm.capabilities.version}")
        KeyValue(
            "Engine",
            if (vm.capabilities.telethon.isNotEmpty()) {
                "Telethon ${vm.capabilities.telethon}"
            } else {
                "Telethon missing"
            },
        )
        KeyValue(
            "FFmpeg",
            if (vm.capabilities.ffmpeg) "available" else "not available",
        )
        TgButton(
            text = "Check for updates",
            kind = TgButtonKind.Secondary,
            onClick = vm::checkForUpdate,
            busy = vm.updateBusy,
        )
        UpdateSection(vm, onInstallUpdate)

        // -- Danger zone
        TgSectionLabel("Danger zone")
        TgNotice(
            text = "This signs you out of Telegram, deletes your saved API " +
                "credentials, and empties the download queue. Files already " +
                "downloaded are not touched. This cannot be undone.",
            kind = TgNoticeKind.Error,
        )
        TgButton(
            text = "Reset everything",
            kind = TgButtonKind.Danger,
            onClick = { showReset = true },
        )
    }

    // The reset asks for the word: a plain yes/no sat one stray click away
    // from wiping a logged-in session.
    if (showReset) {
        AlertDialog(
            onDismissRequest = { showReset = false },
            title = { Text("Reset everything?") },
            text = {
                Column(verticalArrangement = Arrangement.spacedBy(Space.s2)) {
                    Text(
                        text = "This signs you out of Telegram, deletes your " +
                            "saved API credentials, and empties the download " +
                            "queue. Files already downloaded are not touched. " +
                            "This cannot be undone.",
                        color = c.muted,
                        fontSize = Type.meta,
                    )
                    TgTextField(value = phrase, onValueChange = { phrase = it })
                    Text(
                        text = "Type RESET to confirm.",
                        color = c.muted,
                        fontSize = Type.meta,
                    )
                }
            },
            confirmButton = {
                TgButton(
                    text = "Reset everything",
                    kind = TgButtonKind.Danger,
                    enabled = phrase.trim() == "RESET",
                    onClick = {
                        showReset = false
                        phrase = ""
                        vm.resetEverything()
                    },
                )
            },
            dismissButton = {
                TgButton(
                    text = "Cancel",
                    kind = TgButtonKind.Quiet,
                    onClick = {
                        showReset = false
                        phrase = ""
                    },
                )
            },
        )
    }
}

/**
 * The update, once a check has found one.
 *
 * The bar is the point of this. The download runs in the engine's
 * background, and this is the only thing that says it is happening: a
 * button that goes quiet for half a minute reads as a broken app, and
 * that is exactly what the updater used to look like.
 *
 * The bar is indeterminate until the server says how big the package is,
 * because a bar pinned at zero for the first second reads as "stuck"
 * rather than "starting".
 */
@Composable
private fun UpdateSection(vm: TgViewModel, onInstallUpdate: () -> Unit) {
    val c = tgColors()
    val result = vm.updateResult
    if (!result.ok || !result.newer) return

    TgNotice(
        text = "Telegram Downloader ${result.version} is available.",
        kind = TgNoticeKind.Info,
    )
    val download = vm.updateDownload
    when {
        download.downloading -> {
            TgProgressBar(
                value = if (download.known) download.progress else null,
            )
            Text(
                text = download.detail.ifEmpty { "Downloading the update…" },
                color = c.muted,
                fontSize = Type.meta,
            )
        }
        download.doneOk -> {
            TgButton(
                text = "Install update",
                kind = TgButtonKind.Primary,
                onClick = onInstallUpdate,
            )
        }
        else -> {
            TgButton(
                text = "Download update",
                kind = TgButtonKind.Primary,
                onClick = vm::startUpdateDownload,
            )
        }
    }
    if (download.failed) {
        TgNotice(
            text = download.error.ifEmpty { "The download failed." },
            kind = TgNoticeKind.Error,
        )
    }
}

/**
 * One dependency row: the status dot, the name and the description, and
 * the version and the badge.
 *
 * This is the desktop's row, in the desktop's order: the dot, the
 * middle (which takes the space that is left over) and a trailing box
 * with the version and the badge. The desktop's difference is that its
 * descriptions are one line; on a phone they are paragraphs.
 *
 * The one thing that had to change is the ALIGNMENT. The trailing box
 * sits at the TOP of the row, beside the name, and not in the middle of
 * it: a description that is four lines tall, with the version and the
 * badge centred against it, puts them in the middle of the paragraph
 * and the two run into each other.
 */
@Composable
private fun DependencyLine(dep: DependencyRow) {
    val c = tgColors()
    Column(
        modifier = Modifier.fillMaxWidth(),
        verticalArrangement = Arrangement.spacedBy(Space.s1),
    ) {
        Row(
            horizontalArrangement = Arrangement.spacedBy(Space.s3),
            verticalAlignment = Alignment.Top,
        ) {
            TgStatusDot(if (dep.ok) "done" else "failed")
            Column(
                modifier = Modifier.weight(1f),
                verticalArrangement = Arrangement.spacedBy(Space.s1),
            ) {
                Text(
                    text = dep.name,
                    color = c.ink,
                    fontSize = Type.body,
                    fontWeight = FontWeight.SemiBold,
                )
                if (dep.hint.isNotEmpty()) {
                    Text(text = dep.hint, color = c.muted, fontSize = Type.meta)
                }
                if (!dep.ok && dep.install.isNotEmpty()) {
                    Text(text = dep.install, color = c.muted, fontSize = Type.meta)
                }
            }
            // The trailing box: the version and the badge, at the top of
            // the row so that they line up with the name.
            Row(
                horizontalArrangement = Arrangement.spacedBy(Space.s2),
                verticalAlignment = Alignment.Top,
            ) {
                Text(
                    text = dep.version.ifEmpty { "not detected" },
                    color = c.muted,
                    fontSize = Type.meta,
                    maxLines = 1,
                    overflow = TextOverflow.Ellipsis,
                )
                TgBadge(
                    text = if (dep.ok) "OK" else "MISSING",
                    tone = if (dep.ok) TgTone.Ok else TgTone.Error,
                )
            }
        }
        TgHairline()
    }
}

@Composable
private fun KeyValue(key: String, value: String) {
    val c = tgColors()
    Row(verticalAlignment = Alignment.CenterVertically) {
        Text(
            text = key,
            color = c.ink,
            fontSize = Type.body,
            modifier = Modifier.weight(1f),
        )
        Text(
            text = value,
            color = c.ink,
            fontSize = Type.body,
            fontWeight = FontWeight.SemiBold,
        )
    }
}
