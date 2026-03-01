from PIL import Image, ImageDraw, ImageFilter, ImageChops
import random, os

os.makedirs('assets', exist_ok=True)

def hex_to_rgb(h):
    h = h.lstrip('#')
    return tuple(int(h[i:i+2], 16) for i in (0, 2, 4))

def create_banner(filename, color1, color2, color3, seed=42):
    random.seed(seed)
    W, H = 600, 200
    img = Image.new('RGB', (W, H), (10, 12, 18))
    draw = ImageDraw.Draw(img)

    c1 = hex_to_rgb(color1)
    c2 = hex_to_rgb(color2)
    c3 = hex_to_rgb(color3)
    bg = (10, 12, 18)

    # Gradient background
    for x in range(W):
        t = x / W
        r = int(bg[0] * (1 - t * 0.3) + c1[0] * t * 0.15)
        g = int(bg[1] * (1 - t * 0.3) + c1[1] * t * 0.15)
        b = int(bg[2] * (1 - t * 0.3) + c1[2] * t * 0.15)
        draw.line([(x, 0), (x, H)], fill=(min(r, 255), min(g, 255), min(b, 255)))

    # Glowing orbs
    overlay = Image.new('RGB', (W, H), (0, 0, 0))
    odraw = ImageDraw.Draw(overlay)
    for _ in range(6):
        cx = random.randint(50, W - 50)
        cy = random.randint(20, H - 20)
        radius = random.randint(30, 80)
        color = random.choice([c1, c2, c3])
        for r in range(radius, 0, -1):
            c = (color[0] // 4, color[1] // 4, color[2] // 4)
            odraw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=c)

    overlay = overlay.filter(ImageFilter.GaussianBlur(20))
    img = ImageChops.add(img, overlay)

    # Subtle grid lines
    draw2 = ImageDraw.Draw(img)
    for x in range(0, W, 40):
        opacity = random.randint(8, 18)
        draw2.line([(x, 0), (x, H)], fill=(opacity, opacity, opacity + 5))
    for y in range(0, H, 40):
        opacity = random.randint(8, 18)
        draw2.line([(0, y), (W, y)], fill=(opacity, opacity, opacity + 5))

    # Accent dots
    for _ in range(15):
        x = random.randint(20, W - 20)
        y = random.randint(10, H - 10)
        color = random.choice([c1, c2])
        brightness = random.uniform(0.3, 0.8)
        r = random.randint(1, 3)
        c = (int(color[0] * brightness), int(color[1] * brightness), int(color[2] * brightness))
        draw2.ellipse([x - r, y - r, x + r, y + r], fill=c)

    # Connecting lines
    points = [(random.randint(20, W - 20), random.randint(10, H - 10)) for _ in range(8)]
    for i in range(len(points) - 1):
        color = random.choice([c1, c2, c3])
        c = (color[0] // 6, color[1] // 6, color[2] // 6)
        draw2.line([points[i], points[i + 1]], fill=c, width=1)

    img.save(os.path.join('assets', filename), quality=85)
    size = os.path.getsize(os.path.join('assets', filename))
    print("Created %s: %d bytes" % (filename, size))


# Market Signal - gold/amber
create_banner('section_market.jpg', '#FFB84D', '#FBBF24', '#F59E0B', seed=1)
# Research - cyan/emerald
create_banner('section_research.jpg', '#06D6A0', '#34D399', '#22D3EE', seed=2)
# Tools - indigo/purple
create_banner('section_tool.jpg', '#6C6FFF', '#8B8EFF', '#A78BFA', seed=3)
# Risk - rose/red
create_banner('section_risk.jpg', '#FF6B8A', '#FB7185', '#F87171', seed=4)
# Opportunity - purple/violet
create_banner('section_opportunity.jpg', '#A78BFA', '#C4B5FD', '#818CF8', seed=5)

print('All banners created!')
