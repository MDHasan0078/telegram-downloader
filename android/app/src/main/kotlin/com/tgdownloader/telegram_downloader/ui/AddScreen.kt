package com.tgdownloader.telegram_downloader.ui

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.ExperimentalLayoutApi
import androidx.compose.foundation.layout.FlowRow
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import com.tgdownloader.telegram_downloader.Space
import com.tgdownloader.telegram_downloader.TgViewModel
import com.tgdownloader.telegram_downloader.Type
import com.tgdownloader.telegram_downloader.tgColors

/** What each output mode does, in words. */
private val MODE_BLURB = mapOf(
    "1" to "Keep the file exactly as Telegram sent it. No conversion, so " +
        "nothing is ever re-encoded.",
    "2" to "Convert to MP4 with a fast encoder preset. Smaller file, quicker to " +
        "process.",
    "3" to "Convert to MP4 with a better compression preset. Smallest file, " +
        "slowest to process.",
)

/**
 * The Add destination: links in, the output mode, and the button that starts.
 *
 * The order is the desktop app's: the link field, the Output mode control
 * with its blurb, then the actions. The mode sits with the links it applies
 * to, so the choice is made in the same breath as the action.
 */
@OptIn(ExperimentalLayoutApi::class)
@Composable
fun AddScreen(
    vm: TgViewModel,
    onLoadTxt: () -> Unit,
) {
    val c = tgColors()
    val mode = vm.settings.outputMode
    val ffmpeg = vm.capabilities.ffmpeg

    TgPage {
        // -- the link field
        Column(verticalArrangement = Arrangement.spacedBy(Space.s1 + Space.s2)) {
            TgFieldLabel(
                text = "Telegram link(s)",
                hint = if (vm.detectedCount > 0) {
                    "${vm.detectedCount} URL${if (vm.detectedCount == 1) "" else "s"} detected"
                } else {
                    null
                },
            )
            TgTextField(
                value = vm.linksText,
                onValueChange = vm::onLinksChanged,
                placeholder = "https://t.me/channel/123\n(one per line)",
                singleLine = false,
                minLines = 5,
            )
        }

        // -- the output mode
        TgSectionLabel("Output mode")
        TgSegmented(
            options = listOf(
                TgSegmentOption("1", "Original"),
                // Fast and Max convert to MP4, which needs ffmpeg. There is
                // none on Android and the app cannot install one, so they are
                // OFF -- and the notice below says so plainly, rather than
                // letting a long download fail at the very end.
                TgSegmentOption("2", "Fast", enabled = ffmpeg),
                TgSegmentOption("3", "Max", enabled = ffmpeg),
            ),
            value = mode,
            onChange = vm::setOutputMode,
        )
        Text(
            text = MODE_BLURB[mode] ?: MODE_BLURB["1"].orEmpty(),
            color = c.muted,
            fontSize = Type.meta,
        )
        if (!ffmpeg) {
            TgNotice(
                "Fast and Max convert the file to MP4, which needs ffmpeg. " +
                    "Android has no ffmpeg and this app cannot install one, " +
                    "so only Original is available.",
            )
        }

        // -- the actions
        FlowRow(
            horizontalArrangement = Arrangement.spacedBy(Space.s2),
            verticalArrangement = Arrangement.spacedBy(Space.s2),
        ) {
            TgButton(
                text = "Add",
                kind = TgButtonKind.Primary,
                onClick = vm::addLinks,
                busy = vm.adding,
            )
            TgButton(
                text = "Load .txt",
                kind = TgButtonKind.Secondary,
                onClick = onLoadTxt,
            )
            TgButton(
                text = "Clear",
                kind = TgButtonKind.Secondary,
                onClick = vm::clearLinks,
            )
        }

        // -- the status line (hidden when there is nothing to say)
        if (vm.statusText.isNotEmpty()) {
            TgStatusLine(vm.statusText)
        }
    }
}
