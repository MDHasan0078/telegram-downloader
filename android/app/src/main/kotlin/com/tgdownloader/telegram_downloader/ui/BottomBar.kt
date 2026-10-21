package com.tgdownloader.telegram_downloader.ui

import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import com.tgdownloader.telegram_downloader.Radius
import com.tgdownloader.telegram_downloader.Space
import com.tgdownloader.telegram_downloader.Type
import com.tgdownloader.telegram_downloader.tgColors

/**
 * The bottom bar: the download folder, and the one place it is changed.
 *
 * It is pinned on every destination, like the desktop app's: the folder is
 * chosen in ONE place, so there is never a second copy of it to disagree.
 */
@Composable
fun TgBottomBar(
    folder: String,
    onChange: () -> Unit,
    modifier: Modifier = Modifier,
) {
    val c = tgColors()
    Row(
        modifier = modifier
            .fillMaxWidth()
            .background(c.raised)
            .padding(horizontal = Space.pagePad, vertical = Space.s2),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Text(
            text = folder.ifEmpty { "(not set)" },
            color = c.ink,
            fontSize = Type.meta,
            maxLines = 1,
            overflow = TextOverflow.Ellipsis,
            modifier = Modifier.weight(1f),
        )
        TgButton(
            text = "Change",
            kind = TgButtonKind.Secondary,
            onClick = onChange,
        )
    }
}

/**
 * The toast: a transient message over the bottom of the screen.
 *
 * It is a floating surface with a hairline -- and an error is marked by
 * the border, not by a slab of red.
 */
@Composable
fun TgToast(
    text: String,
    isError: Boolean,
    modifier: Modifier = Modifier,
) {
    val c = tgColors()
    val shape = RoundedCornerShape(Radius.md)
    Box(
        modifier = modifier
            .padding(Space.s3)
            .clip(shape)
            .background(c.floating)
            .border(
                1.dp,
                if (isError) c.error.copy(alpha = 0.45f) else c.hairline,
                shape,
            )
            .padding(horizontal = Space.s3 + Space.s2, vertical = Space.s2 + Space.s2),
    ) {
        Text(text = text, color = c.ink, fontSize = Type.body)
    }
}
