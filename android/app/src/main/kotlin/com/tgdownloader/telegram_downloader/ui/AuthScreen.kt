package com.tgdownloader.telegram_downloader.ui

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.ExperimentalLayoutApi
import androidx.compose.foundation.layout.FlowRow
import androidx.compose.foundation.layout.Row
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import com.tgdownloader.telegram_downloader.Space
import com.tgdownloader.telegram_downloader.TgViewModel
import com.tgdownloader.telegram_downloader.Type
import com.tgdownloader.telegram_downloader.tgColors

/**
 * The Account auth destination: the API credentials and the Telegram login.
 *
 * This is where the credentials live -- it is its own destination, not a
 * row buried in Settings -- so the copy that sends the user here ("First add
 * your Telegram API ID + hash in Account auth") and the copy here agree.
 */
@OptIn(ExperimentalLayoutApi::class)
@Composable
fun AuthScreen(vm: TgViewModel) {
    val c = tgColors()
    var apiId by rememberSaveable { mutableStateOf(vm.settings.apiId) }
    var apiHash by rememberSaveable { mutableStateOf(vm.settings.apiHash) }
    var reveal by remember { mutableStateOf(false) }
    var showPhone by remember { mutableStateOf(false) }
    var phone by remember { mutableStateOf("") }
    var code by remember { mutableStateOf("") }
    var password by remember { mutableStateOf("") }

    TgPage {
        TgSectionLabel(
            text = "Telegram account",
            trailing = {
                TgBadge(if (vm.settings.hasApi) "STORED" else "UNSET")
            },
        )
        if (!vm.settings.hasApi) {
            TgNotice(
                "Create these at my.telegram.org → API development tools. " +
                    "They are stored privately (owner-only) and only asked once.",
            )
        }

        Column(verticalArrangement = Arrangement.spacedBy(Space.s1 + Space.s2)) {
            TgFieldLabel("API ID", "my.telegram.org → App api_id")
            TgTextField(value = apiId, onValueChange = { apiId = it })
        }

        Column(verticalArrangement = Arrangement.spacedBy(Space.s1 + Space.s2)) {
            TgFieldLabel("API hash", "128-bit hex string")
            Row(
                verticalAlignment = Alignment.CenterVertically,
                horizontalArrangement = Arrangement.spacedBy(Space.s2),
            ) {
                TgTextField(
                    value = apiHash,
                    onValueChange = { apiHash = it },
                    password = !reveal,
                    modifier = Modifier.weight(1f),
                )
                TgButton(
                    text = if (reveal) "Hide" else "Show",
                    kind = TgButtonKind.Quiet,
                    onClick = { reveal = !reveal },
                )
            }
        }

        FlowRow(
            horizontalArrangement = Arrangement.spacedBy(Space.s2),
            verticalArrangement = Arrangement.spacedBy(Space.s2),
        ) {
            TgButton(
                text = "Save",
                kind = TgButtonKind.Primary,
                onClick = { vm.saveCredentials(apiId, apiHash) },
                busy = vm.authBusy,
            )
            TgButton(
                text = "Verify",
                kind = TgButtonKind.Secondary,
                onClick = { vm.verifyCredentials() },
                busy = vm.authBusy,
            )
            TgButton(
                text = "Log in",
                kind = TgButtonKind.Secondary,
                onClick = { showPhone = true },
                busy = vm.authBusy,
            )
            TgButton(
                text = "Log out",
                kind = TgButtonKind.Secondary,
                onClick = { vm.logout() },
                busy = vm.authBusy,
            )
        }

        if (vm.settings.userId.isNotEmpty()) {
            Text(
                text = "Pinned to Telegram user ${vm.settings.userId}.",
                color = c.muted,
                fontSize = Type.meta,
            )
        }
    }

    // -- the login handshake ----------------------------------------------
    // Telegram asks for a phone number, then a code, and sometimes a 2FA
    // password. Each step is its own dialog; the engine decides which one
    // comes next.
    if (showPhone) {
        AlertDialog(
            onDismissRequest = { showPhone = false },
            title = { Text("Telegram login") },
            text = {
                Column(verticalArrangement = Arrangement.spacedBy(Space.s2)) {
                    Text(
                        text = "Enter your phone number (with the country code) " +
                            "or a bot token.",
                        color = c.muted,
                        fontSize = Type.meta,
                    )
                    TgTextField(value = phone, onValueChange = { phone = it })
                }
            },
            confirmButton = {
                TgButton(
                    text = "Continue",
                    kind = TgButtonKind.Primary,
                    onClick = {
                        showPhone = false
                        vm.loginBegin(phone)
                    },
                )
            },
            dismissButton = {
                TgButton(
                    text = "Cancel",
                    kind = TgButtonKind.Quiet,
                    onClick = { showPhone = false },
                )
            },
        )
    }

    if (vm.loginState == "code") {
        AlertDialog(
            onDismissRequest = { },
            title = { Text("Enter the code we just sent") },
            text = {
                Column(verticalArrangement = Arrangement.spacedBy(Space.s2)) {
                    Text(
                        text = "Telegram sent a login code to your phone.",
                        color = c.muted,
                        fontSize = Type.meta,
                    )
                    TgTextField(value = code, onValueChange = { code = it })
                }
            },
            confirmButton = {
                TgButton(
                    text = "Sign in",
                    kind = TgButtonKind.Primary,
                    onClick = { vm.loginCode(code) },
                )
            },
        )
    }

    if (vm.loginState == "password") {
        AlertDialog(
            onDismissRequest = { },
            title = { Text("2-step verification") },
            text = {
                Column(verticalArrangement = Arrangement.spacedBy(Space.s2)) {
                    Text(
                        text = "This account has two-step verification on. " +
                            "Enter the password.",
                        color = c.muted,
                        fontSize = Type.meta,
                    )
                    TgTextField(
                        value = password,
                        onValueChange = { password = it },
                        password = true,
                    )
                }
            },
            confirmButton = {
                TgButton(
                    text = "Sign in",
                    kind = TgButtonKind.Primary,
                    onClick = { vm.loginPassword(password) },
                )
            },
        )
    }
}
