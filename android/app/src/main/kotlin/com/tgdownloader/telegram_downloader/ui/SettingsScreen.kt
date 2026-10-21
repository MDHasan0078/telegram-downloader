package com.tgdownloader.telegram_downloader.ui

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.padding
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
fun SettingsScreen(vm: TgViewModel) {
    val c = tgColors()
    var showReset by remember { mutableStateOf(false) }
    var phrase by remember { mutableStateOf("") }

    TgPage {
        // -- Dependencies
        TgSectionLabel("Dependencies")
        if (vm.dependencies.isEmpty()) {
            Text(
                text = "Checking…",
                color = c.muted,
                fontSize = Type.meta,
            )
        }
        for (dep in vm.dependencies) {
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
 * One dependency row: the status dot, the name, the version and the badge.
 *
 * A MISSING row gets its own line with the truth about this platform --
 * above all, that ffmpeg cannot be installed on Android, so the Fast and
 * Max modes are off.
 */
@Composable
private fun DependencyLine(dep: DependencyRow) {
    val c = tgColors()
    Column(
        modifier = Modifier.fillMaxWidth(),
        verticalArrangement = Arrangement.spacedBy(Space.s1),
    ) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            TgStatusDot(if (dep.ok) "done" else "failed")
            Column(
                modifier = Modifier
                    .weight(1f)
                    .padding(start = Space.s3),
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
            }
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
        if (!dep.ok && dep.install.isNotEmpty()) {
            Text(
                text = dep.install,
                color = c.muted,
                fontSize = Type.meta,
                modifier = Modifier.padding(start = Space.s3 + Space.s3),
            )
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
