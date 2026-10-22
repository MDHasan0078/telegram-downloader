package com.tgdownloader.telegram_downloader.ui

import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.horizontalScroll
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.BoxWithConstraints
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.relocation.BringIntoViewRequester
import androidx.compose.foundation.relocation.bringIntoViewRequester
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.remember
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.draw.rotate
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.semantics
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
 * The window width at which the bar shows the FULL wordmark.
 *
 * 600dp is not a taste, it is the arithmetic of the desktop's bar: the
 * brand (141dp: the mark, the gap and the name), the divider and the
 * two gaps around it (17dp), the four destinations and the theme toggle
 * with their gaps (388dp), and the window gutter (48dp) -- 594dp. At
 * 600dp the whole bar fits on one line, which is exactly what the
 * desktop shows.
 *
 * A phone is 420dp wide, and the wordmark alone is 124dp of it: 30% of
 * the screen for a label, with five things to reach besides it. Below
 * this width the bar keeps the MARK and gives the rest to the
 * destinations.
 */
private val fullWordmarkWidth = 600.dp

/**
 * The width at which the chips get the desktop's roomy padding.
 *
 * On a phone the four destinations and the theme toggle are about
 * 350dp of chip, and the space they have is about 350dp as well: the
 * last one hangs off the end of the strip. So the bar scrolls to reach
 * "Settings", and a chip that is half off the strip's left edge shows
 * the last letter of its label -- a bare "d" from "Add" -- sitting
 * next to the brand mark.
 *
 * The tighter padding is 4dp a side, which across four chips is 32dp:
 * the difference between fitting and not. The desktop's padding is
 * roomy because a desktop window has the room.
 */
private val roomyChipsWidth = 600.dp

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
 * THE BRAND IS A FIXED SLOT, outside the scrolling strip, and that is
 * the whole point of it. The brand used to be the first thing IN the
 * strip, and the strip scrolled it off the left edge: the bar opened on
 * a bare "Add", with nothing on screen to say what the app was. A
 * wordmark that can scroll out of view is not a wordmark.
 *
 * The destinations scroll in what is left, and the selected one is
 * brought back into view when the destination changes.
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

    BoxWithConstraints(
        modifier = modifier
            .fillMaxWidth()
            .background(c.page),
    ) {
        // A narrow bar cannot afford the name: it keeps the mark, and the
        // destinations get the screen. A wide one shows the desktop's bar
        // exactly.
        val fullWordmark = maxWidth >= fullWordmarkWidth
        // And a narrow bar cannot afford the desktop's padding either:
        // with it, the last destination hangs off the end of the strip.
        val roomyChips = maxWidth >= roomyChipsWidth

        Row(
            modifier = Modifier
                .fillMaxWidth()
                .padding(horizontal = Space.pagePad, vertical = Space.s2),
            verticalAlignment = Alignment.CenterVertically,
            horizontalArrangement = Arrangement.spacedBy(Space.s2),
        ) {
            // The brand never scrolls.
            TgBrand(fullWordmark = fullWordmark)

            Row(
                modifier = Modifier
                    .weight(1f)
                    .horizontalScroll(scroll),
                verticalAlignment = Alignment.CenterVertically,
                horizontalArrangement = Arrangement.spacedBy(Space.s2),
            ) {
                Destination.Add.chip(selected, onSelect, roomyChips)
                Destination.Ongoing.chip(selected, onSelect, roomyChips)
                // The gap the desktop bar flexes: on a phone it is a fixed
                // space, so the theme toggle keeps its air on both sides.
                Box(modifier = Modifier.size(Space.s2))
                TgThemeToggle(dark = dark, onToggleTheme = onToggleTheme)
                Destination.Auth.chip(selected, onSelect, roomyChips)
                Destination.Settings.chip(selected, onSelect, roomyChips)
            }
        }
    }
}

/**
 * One destination chip.
 *
 * The chip asks to be brought into view when it becomes the selected
 * one, and Compose's own relocation API does the scrolling: it moves
 * the strip by the minimum that puts the chip on screen and not one
 * pixel more.
 *
 * The old hand-rolled arithmetic -- `positionInParent()` plus a
 * viewport width -- was the bug. It asked the strip to scroll to the
 * selected chip's LEFT EDGE, which for the chip near the end of the
 * strip is further right than the strip can go: the strip ran to its
 * end, and the brand, which was the first thing in it, went off the
 * left edge with it.
 */
@Composable
private fun Destination.chip(
    selected: Destination,
    onSelect: (Destination) -> Unit,
    roomy: Boolean,
) {
    val c = tgColors()
    val shape = RoundedCornerShape(Radius.sm)
    val isSelected = this == selected
    val requester = remember { BringIntoViewRequester() }

    // The selected destination is always on screen.
    LaunchedEffect(isSelected) {
        if (isSelected) requester.bringIntoView()
    }

    Box(
        modifier = Modifier
            .bringIntoViewRequester(requester)
            .clip(shape)
            .background(if (isSelected) c.raised else Color.Transparent)
            .border(
                1.dp,
                if (isSelected) c.hairline else Color.Transparent,
                shape,
            )
            .clickable { onSelect(this) }
            .padding(
                horizontal = if (roomy) Space.s3 else Space.s2,
                vertical = Space.s1 + 2.dp,
            ),
    ) {
        Text(
            text = title,
            color = if (isSelected) c.ink else c.muted,
            fontSize = Type.body,
            fontWeight = if (isSelected) FontWeight.SemiBold else FontWeight.Medium,
        )
    }
}

/**
 * The brand: the mark, and -- when the bar is wide enough for it -- the
 * name.
 *
 * This is the desktop's brand, in the desktop's order. It is a fixed
 * slot, so it is always on screen.
 *
 * The divider is NOT inside the "wide enough" branch. On a phone the
 * strip is a couple of dp wider than the space it has, so it scrolls,
 * and a chip that is half off its left edge shows the last letter of
 * its label -- a bare "d" from "Add" -- right next to the mark. With
 * the divider there, that letter is on the other side of a rule and
 * reads as a chip the strip is part-way through, which is what it is.
 * The desktop has the same divider in the same place.
 */
@Composable
private fun TgBrand(fullWordmark: Boolean) {
    val c = tgColors()
    Row(
        verticalAlignment = Alignment.CenterVertically,
        horizontalArrangement = Arrangement.spacedBy(Space.s2),
    ) {
        TgBrandMark(announce = !fullWordmark)
        if (fullWordmark) {
            Text(
                text = "Telegram Downloader",
                color = c.ink,
                fontSize = Type.title,
                fontWeight = FontWeight.SemiBold,
            )
        }
        // The desktop's divider between the brand and the destinations.
        TgHairline(vertical = true)
    }
}

/**
 * The mark: the app's accent square, turned.
 *
 * When the name is not on screen the mark carries it as its
 * accessibility label, so the brand is still announced -- and so the
 * launcher can see that the bar has a wordmark.
 */
@Composable
private fun TgBrandMark(announce: Boolean) {
    val c = tgColors()
    Box(
        modifier = Modifier
            .size(9.dp)
            .rotate(45f)
            .clip(RoundedCornerShape(1.dp))
            .background(c.accent)
            .then(
                if (announce) {
                    Modifier.semantics { contentDescription = "Telegram Downloader" }
                } else {
                    Modifier
                }
            ),
    )
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
