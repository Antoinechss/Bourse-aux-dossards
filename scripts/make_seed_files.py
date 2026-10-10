"""Fabrique les fichiers de démonstration de seed/ : les règlements en PDF et de faux justificatifs.

À relancer depuis la racine du projet après avoir modifié un règlement (.txt) :
    uv run python -m scripts.make_seed_files

Les justificatifs sont des spécimens fictifs, marqués comme tels sur le document.
"""

from pathlib import Path

import reportlab
from PIL import Image, ImageDraw, ImageFont
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.pdfgen.canvas import Canvas
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer

SEED = Path(__file__).parent.parent / "seed"
SPECIMEN = "SPÉCIMEN — document fictif de démonstration"
# La police par défaut de Pillow n'a pas les accents : on prend celle livrée avec reportlab.
FONT = Path(reportlab.__file__).parent / "fonts" / "Vera.ttf"

PROOFS = [
    {
        "file": "licence-ffa-lea-moreau.png",
        "title": "Licence FFA — Athlé Running",
        "lines": [
            "Nom : MOREAU",
            "Prénom : Léa",
            "Née le : 14/02/1994",
            "N° de licence : 2345678",
            "Club : AC Port-Clair",
            "Saison : 2026-2027",
            "Fin de validité : 31/08/2027",
        ],
    },
    {
        "file": "pps-karim-benali.pdf",
        "title": "Parcours de Prévention Santé (PPS) — Attestation",
        "lines": [
            "Nom : BENALI",
            "Prénom : Karim",
            "Né le : 03/09/1988",
            "N° PPS : P7K2M9QX4",
            "Délivré le : 12/02/2027",
            "Valable jusqu'au : 12/05/2027",
        ],
    },
    {
        "file": "pps-expire-hugo-lemaire.jpg",
        "title": "Parcours de Prévention Santé (PPS) — Attestation",
        "lines": [
            "Nom : LEMAIRE",
            "Prénom : Hugo",
            "Né le : 21/06/1991",
            "N° PPS : R4T8W2ZB6",
            "Délivré le : 05/10/2026",
            "Valable jusqu'au : 05/01/2027",
        ],
    },
    {
        "file": "carte-salle-de-sport.png",
        "title": "FitClub Valombre — Carte d'adhérent",
        "lines": [
            "Nom : MOREAU",
            "Prénom : Léa",
            "Abonnement : annuel",
            "Valable jusqu'au : 31/12/2027",
        ],
    },
]


def make_reglement(source: Path) -> Path:
    """Met en page un règlement : titre, sous-titre, puis les articles séparés par une ligne vide."""
    styles = getSampleStyleSheet()
    header, *articles = source.read_text(encoding="utf-8").strip().split("\n\n")
    title, subtitle = header.split("\n")
    story = [Paragraph(title, styles["Title"]), Paragraph(subtitle, styles["Italic"]), Spacer(1, 0.6 * cm)]
    for article in articles:
        heading, _, body = article.partition("\n")
        story += [Paragraph(heading, styles["Heading3"]), Paragraph(body, styles["BodyText"])]
    target = source.with_suffix(".pdf")
    # invariant : pas de date de création dans le fichier, donc le même texte donne le même PDF.
    SimpleDocTemplate(
        str(target), pagesize=A4, leftMargin=2.2 * cm, rightMargin=2.2 * cm, title=title, invariant=1
    ).build(story)
    return target


def make_proof_pdf(target: Path, title: str, lines: list[str]) -> None:
    canvas = Canvas(str(target), pagesize=A4, invariant=1)
    canvas.setTitle(title)
    canvas.setFillColorRGB(0.75, 0.1, 0.1)
    canvas.setFont("Helvetica-Bold", 13)
    canvas.drawString(2.5 * cm, 27 * cm, SPECIMEN)
    canvas.setFillColorRGB(0, 0, 0)
    canvas.setFont("Helvetica-Bold", 18)
    canvas.drawString(2.5 * cm, 25.2 * cm, title)
    canvas.setFont("Helvetica", 13)
    for index, line in enumerate(lines):
        canvas.drawString(2.5 * cm, (23.5 - index) * cm, line)
    canvas.save()


def make_proof_image(target: Path, title: str, lines: list[str]) -> None:
    image = Image.new("RGB", (1100, 700), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((20, 20, 1080, 680), outline="black", width=3)
    draw.text((60, 50), SPECIMEN, fill=(190, 25, 25), font=ImageFont.truetype(FONT, 26))
    draw.text((60, 120), title, fill="black", font=ImageFont.truetype(FONT, 38))
    for index, line in enumerate(lines):
        draw.text((60, 220 + 58 * index), line, fill="black", font=ImageFont.truetype(FONT, 30))
    image.save(target)


def main() -> None:
    for source in sorted((SEED / "reglements").glob("*.txt")):
        print("règlement    :", make_reglement(source).name)
    proofs = SEED / "justificatifs"
    proofs.mkdir(exist_ok=True)
    for proof in PROOFS:
        target = proofs / proof["file"]
        make = make_proof_pdf if target.suffix == ".pdf" else make_proof_image
        make(target, proof["title"], proof["lines"])
        print("justificatif :", target.name)


if __name__ == "__main__":
    main()
