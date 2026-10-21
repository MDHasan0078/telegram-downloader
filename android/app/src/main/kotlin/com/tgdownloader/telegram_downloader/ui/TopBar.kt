package com.tgdownloader.telegram_downloader.ui

import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.horizontalScroll
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateMapOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.draw.rotate
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.layout.onGloballyPositioned
import androidx.compose.ui.layout.onSizeChanged
import androidx.compose.ui.layout.positionInParent
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import com.tgdownloader.telegram_downloader.Radius
import com.tgdownloader.telegram_downloader.Space
import com.tgdownloader.telegram_downloader.Type
import com.tgdownloader.telegram_downloader.tgColors

/** The four destinations, in the order the bar shows them. */
enum class Destination(val key: String, val title: String) {
    Add("add", "Add"),
    Ongoing("ongoing", "Ongoing"),
    Auth("auth", "Account auth"),
    Settings("settings", "Settings"),
}

/**
 * The top bar: the brand, the tab strip, and the theme toggle.
 *
 * The bar IS the navigation, and there is no per-page heading: the chip
 * strip is the page title. The selected chip is a RAISED surface with a
 * hairline -- never an accent fill, because colour in this app means action
 * or status, not "where am I".
 *
 * The order is the desktop app's:
 *
 *     [brand] |  Add  Ongoing  ......  [theme]  Account auth  Settings
 *
 * The theme toggle sits in the gap because it is chrome, not a destination,
 * and it gets air on both sides so it never reads as a fifth tab.
 *
 * On a phone the row is wider than the screen, so it scrolls -- and the
 * selected chip is scrolled back into view when the destination changes.
 */
@Composable
fun TgTopBar(
    selected: Destination,
    onSelect: (Destination) -> Unit,
    dark: Boolean,
    onToggleTheme: () -> Unit,
    modifier: Modifier = Modifier,
) {
    val c = tgColors()
    val scroll = rememberScrollState()
    val chipOffsets = remember { mutableStateMapOf<String, Int>() }
    val chipWidths = remember { mutableStateMapOf<String, Int>() }
    var viewport by remember { mutableStateOf(0) }

    LaunchedEffect(selected, chipOffsets.size) {
        val x = chipOffsets[selected.key] ?: return@LaunchedEffect
        val w = chipWidths[selected.key] ?: 0
        if (x < scroll.value || x + w > scroll.value + viewport) {
            scroll.animateScrollTo(x)
        }
    }

    Row(
        modifier = modifier
            .fillMaxWidth()
            .background(c.page)
            .onSizeChanged { viewport = it.width }
            .padding(horizontal = Space.pagePad, vertical = Space.s2),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Row(
            modifier = Modifier
                .weight(1f)
                .horizontalScroll(scroll),
            verticalAlignment = Alignment.CenterVertically,
            horizontalArrangement = Arrangement.spacedBy(Space.s2),
        ) {
            TgBrand()
            TgHairline(vertical = true)
            Destination.Add.chip(selected, onSelect, chipOffsets, chipWidths)
            Destination.Ongoing.chip(selected, onSelect, chipOffsets, chipWidths)
            // The gap the desktop bar flexes: on a phone it is a fixed
            // space, so the theme toggle keeps its air on both sides.
            Box(modifier = Modifier.size(Space.s2))
            TgThemeToggle(dark = dark, onToggleTheme = onToggleTheme)
            Destination.Auth.chip(selected, onSelect, chipOffsets, chipWidths)
            Destination.Settings.chip(selected, onSelect, chipOffsets, chipWidths)
        }
    }
}

/** One destination chip, wired to the bar's scroll bookkeeping. */
@Composable
private fun Destination.chip(
    selected: Destination,
    onSelect: (Destination) -> Unit,
    offsets: MutableMap<String, Int>,
    widths: MutableMap<String, Int>,
) {
    val c = tgColors()
    val shape = RoundedCornerShape(Radius.sm)
    val isSelected = this == selected
    Box(
        modifier = Modifier
            .onGloballyPositioned {
                offsets[key] = it.positionInParent().x
                widths[key] = it.size.width
            }
            .clip(shape)
            .background(if (isSelected) c.raised else Color.Transparent)
            .border(
                1.dp,
                if (isSelected) c.hairline else Color.Transparent,
                shape,
            )
            .clickable { onSelect(this) }
            .padding(horizontal = Space.s3, vertical = Space.s1 + 2.dp),
    ) {
        Text(
            text = title,
            color = if (isSelected) c.ink else c.muted,
            fontSize = Type.body,
            fontWeight = if (isSelected) FontWeight.SemiBold else FontWeight.Medium,
        )
    }
}

/** The wordmark: the mark and the name. */
@Composable
private fun TgBrand() {
    val c = tgColors()
    Row(
        verticalAlignment = Alignment.CenterVertically,
        horizontalArrangement = Arrangement.spacedBy(Space.s2),
    ) {
        Box(
            modifier = Modifier
                .size(9.dp)
                .rotate(45f)
                .clip(RoundedCornerShape(1.dp))
                .background(c.accent),
        )
        Text(
            text = "Telegram Downloader",
            color = c.ink,
            fontSize = Type.title,
            fontWeight = FontWeight.SemiBold,
        )
    }
}

/** The theme toggle: chrome, not a destination. */
@Composable
private fun TgThemeToggle(dark: Boolean, onToggleTheme: () -> Unit) {
    val c = tgColors()
    Box(
        modifier = Modifier
            .size(28.dp)
            .clip(CircleShape)
            .clickable { onToggleTheme() },
        contentAlignment = Alignment.Center,
    ) {
        // The glyph shows the theme the button switches TO.
        Text(
            text = if (dark) "☀" else "☾",
            color = c.muted,
            fontSize = Type.body,
        )
    }
}
