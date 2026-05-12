from PIL import Image, ImageDraw, ImageFont
import os

_FONT_PATH_BOLD   = '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf'
_FONT_PATH_NORMAL = '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf'

def _font(size, bold=False):
    path = _FONT_PATH_BOLD if bold else _FONT_PATH_NORMAL
    try:
        return ImageFont.truetype(path, size)
    except Exception:
        return ImageFont.load_default()

def generate_win_card(username: str, game: str, amount: float,
                      multiplier: float = None) -> bytes:
    W, H = 600, 320
    img  = Image.new('RGB', (W, H), color='#0a0a0f')
    d    = ImageDraw.Draw(img)

    # Background gradient-like border
    for i in range(4):
        d.rectangle([i, i, W - 1 - i, H - 1 - i], outline='#f5c842')

    # Subtle inner glow lines
    d.rectangle([6, 6, W - 7, H - 7], outline='#2a2a1a')

    # Game icon row
    game_icons = {
        'slots': '🎰', 'crash': '🚀', 'blackjack': '🃏',
        'bingo': '🅱️', 'keno': '🎱', 'highlow': '🔼',
        'poker': '♠️', 'tower': '🗼',
    }
    icon = game_icons.get(game, '💰')

    # Title
    d.text((W // 2, 40),  'LIELĀ UZVARA!',
           font=_font(32, bold=True), fill='#f5c842', anchor='mm')

    # Amount
    amount_str = f'+{amount:,.0f} coins'
    d.text((W // 2, 110), amount_str,
           font=_font(48, bold=True), fill='#00e676', anchor='mm')

    # Multiplier if present
    if multiplier:
        d.text((W // 2, 165), f'@ {multiplier:.2f}x',
               font=_font(24), fill='#ffffff', anchor='mm')

    # Username + game
    d.text((W // 2, 210), f'{username}  •  {game.upper()}',
           font=_font(18), fill='#888888', anchor='mm')

    # Site watermark
    d.text((W // 2, 280), 'novakods.lv',
           font=_font(14), fill='#333333', anchor='mm')

    import io
    buf = io.BytesIO()
    img.save(buf, format='PNG', optimize=True)
    return buf.getvalue()