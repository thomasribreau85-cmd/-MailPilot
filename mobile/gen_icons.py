#!/usr/bin/env python3
"""
Génère les icônes PNG pour iOS et Android depuis icon.svg
Nécessite : pip install cairosvg
"""
import subprocess, sys, os

def install(pkg):
    subprocess.check_call([sys.executable, '-m', 'pip', 'install', pkg, '-q'])

try:
    import cairosvg
except ImportError:
    print("Installation de cairosvg...")
    install('cairosvg')
    import cairosvg

SVG_SRC = os.path.join(os.path.dirname(__file__), '..', 'static', 'icon.svg')
OUT_DIR = os.path.join(os.path.dirname(__file__), 'assets')
os.makedirs(OUT_DIR, exist_ok=True)

# Tailles requises par Capacitor Assets
SIZES = {
    'icon.png': 1024,          # source principale
    'icon-foreground.png': 1024,
    'splash.png': 2732,
    'splash-dark.png': 2732,
}

with open(SVG_SRC, 'rb') as f:
    svg_data = f.read()

for filename, size in SIZES.items():
    out_path = os.path.join(OUT_DIR, filename)

    if 'splash' in filename:
        # Splash : fond coloré + icône centrée
        # On crée un SVG de splash
        splash_svg = f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {size} {size}">
  <rect width="{size}" height="{size}" fill="#060b18"/>
  <g transform="translate({size//2 - 256},{size//2 - 256})">
    <rect width="512" height="512" rx="110" ry="110" fill="url(#bg)"/>
    <defs>
      <linearGradient id="bg" x1="0%" y1="0%" x2="100%" y2="100%">
        <stop offset="0%" style="stop-color:#4f6ef7"/>
        <stop offset="100%" style="stop-color:#7c4ff8"/>
      </linearGradient>
    </defs>
    <rect x="96" y="168" width="320" height="200" rx="18" ry="18" fill="none" stroke="white" stroke-width="22"/>
    <polyline points="96,186 256,296 416,186" fill="none" stroke="white" stroke-width="22" stroke-linejoin="round"/>
  </g>
</svg>'''
        cairosvg.svg2png(bytestring=splash_svg.encode(), write_to=out_path, output_width=size, output_height=size)
    else:
        cairosvg.svg2png(bytestring=svg_data, write_to=out_path, output_width=size, output_height=size)

    print(f"  ✅ {filename} ({size}x{size})")

print(f"\nIcones générées dans {OUT_DIR}/")
print("Lance maintenant : npm run icons")
