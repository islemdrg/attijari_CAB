"""
report_pdf.py
=============
Génère un rapport PDF détaillé pour un ticket CAB : scores, explications
(avec barème en points), analyse, preuves et recommandations.

Utilisé par la route /ticket/<id>/export de web_app.py.

"""

from datetime import datetime
from io import BytesIO
from collections import Counter

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    BaseDocTemplate,
    Frame,
    HRFlowable,
    ListFlowable,
    ListItem,
    PageTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
)


# ============================================================
# CHARTE GRAPHIQUE (reprend attijari.css :root exactement)
# ============================================================

ACCENT = colors.HexColor("#e84d45")
ACCENT_DARK = colors.HexColor("#cf4038")
ACCENT_SOFT = colors.HexColor("#fff1f0")

NAVY = colors.HexColor("#202124")
TEXT = colors.HexColor("#202124")
TEXT_SOFT = colors.HexColor("#5f6368")
TEXT_MUTED = colors.HexColor("#8b8d91")
BORDER = colors.HexColor("#e9e9ea")
SURFACE_MUTED = colors.HexColor("#fafafa")

SUCCESS = colors.HexColor("#168260")
WARNING = colors.HexColor("#9b6800")
DANGER = colors.HexColor("#b83345")
NEUTRAL = colors.HexColor("#696c71")

LEVEL_EXCELLENT = colors.HexColor("#6337a6")
LEVEL_GOOD = colors.HexColor("#167852")
LEVEL_MEDIUM = colors.HexColor("#926000")
LEVEL_LOW = colors.HexColor("#b62d42")
LEVEL_NEUTRAL = colors.HexColor("#60636a")


def _score_color(score):
    try:
        value = float(score)
    except (TypeError, ValueError):
        return NEUTRAL
    if value >= 80:
        return SUCCESS
    if value >= 60:
        return colors.HexColor("#886000")
    if value >= 40:
        return WARNING
    return DANGER


def _level_color(level):
    value = str(level or "").strip().lower()
    if "excellent" in value:
        return LEVEL_EXCELLENT
    if "bon" in value:
        return LEVEL_GOOD
    if "moyen" in value:
        return LEVEL_MEDIUM
    if "faible" in value:
        return LEVEL_LOW
    return LEVEL_NEUTRAL


# ============================================================
# STYLES
# ============================================================

_base = getSampleStyleSheet()

STYLES = {
    "report_title": ParagraphStyle(
        "report_title", parent=_base["Title"],
        fontName="Helvetica-Bold", fontSize=19, leading=23,
        textColor=NAVY, alignment=TA_LEFT, spaceAfter=3,
    ),
    "report_subtitle": ParagraphStyle(
        "report_subtitle", parent=_base["Normal"],
        fontName="Helvetica", fontSize=10, leading=14,
        textColor=TEXT_SOFT, alignment=TA_LEFT,
    ),
    "eyebrow": ParagraphStyle(
        "eyebrow", parent=_base["Normal"],
        fontName="Helvetica-Bold", fontSize=8, leading=10,
        textColor=ACCENT, alignment=TA_LEFT, spaceAfter=4,
    ),
    "section_heading": ParagraphStyle(
        "section_heading", parent=_base["Heading2"],
        fontName="Helvetica-Bold", fontSize=12.5, leading=16,
        textColor=NAVY, spaceBefore=18, spaceAfter=9,
    ),
    "body": ParagraphStyle(
        "body", parent=_base["Normal"],
        fontName="Helvetica", fontSize=9.5, leading=14.5,
        textColor=TEXT, alignment=TA_LEFT,
    ),
    "body_muted": ParagraphStyle(
        "body_muted", parent=_base["Normal"],
        fontName="Helvetica-Oblique", fontSize=9, leading=13,
        textColor=TEXT_MUTED,
    ),
    "label": ParagraphStyle(
        "label", parent=_base["Normal"],
        fontName="Helvetica-Bold", fontSize=8, leading=10,
        textColor=TEXT_MUTED, alignment=TA_CENTER,
    ),
    "list_item": ParagraphStyle(
        "list_item", parent=_base["Normal"],
        fontName="Helvetica", fontSize=9.5, leading=14.5,
        textColor=TEXT,
    ),
    "footer": ParagraphStyle(
        "footer", parent=_base["Normal"],
        fontName="Helvetica", fontSize=7.5,
        textColor=TEXT_MUTED, alignment=TA_CENTER,
    ),
}


# ============================================================
# HELPERS
# ============================================================

def _txt(value, fallback="—"):
    if value is None:
        return fallback
    text = str(value).strip()
    if not text or text.lower() in {"nan", "none"}:
        return fallback
    return text


def _num(value, fallback="—"):
    try:
        return str(int(round(float(value))))
    except (TypeError, ValueError):
        return fallback


def _bullet_list(items, empty_text):
    items = [i for i in (items or []) if str(i).strip()]
    if not items:
        return Paragraph(empty_text, STYLES["body_muted"])
    return ListFlowable(
        [ListItem(Paragraph(str(item), STYLES["list_item"]), spaceBefore=4) for item in items],
        bulletType="bullet",
        bulletFontSize=6,
        bulletColor=ACCENT,
        leftIndent=14,
    )


def _draw_logo_mark(canvas, x, y):
    """Bloc-marque vectoriel 'attijari bank', sans dépendre d'un fichier
    image externe. Pour le vrai logo PNG/JPG, remplacer par :
        canvas.drawImage(str(logo_path), x, y - 8*mm, width=22*mm,
                          height=8*mm, preserveAspectRatio=True, mask="auto")
    """
    canvas.saveState()
    canvas.setFillColor(colors.white)
    canvas.roundRect(x, y - 7.2 * mm, 6.5 * mm, 6.5 * mm, 1.4, stroke=0, fill=1)
    canvas.setFillColor(ACCENT)
    canvas.roundRect(x + 1.1 * mm, y - 6.1 * mm, 4.3 * mm, 4.3 * mm, 1, stroke=0, fill=1)
    canvas.setFillColor(colors.white)
    canvas.setFont("Helvetica-Bold", 14)
    canvas.drawString(x + 9 * mm, y - 6.4 * mm, "attijari")
    canvas.setFillColor(colors.HexColor("#ffd9d6"))
    canvas.setFont("Helvetica-Bold", 14)
    canvas.drawString(x + 9 * mm + canvas.stringWidth("attijari", "Helvetica-Bold", 14), y - 6.4 * mm, " bank")
    canvas.restoreState()


def _header_footer(canvas, doc):
    canvas.saveState()

    canvas.setFillColor(NAVY)
    canvas.rect(0, A4[1] - 17 * mm, A4[0], 17 * mm, stroke=0, fill=1)
    canvas.setFillColor(ACCENT)
    canvas.rect(0, A4[1] - 17.9 * mm, A4[0], 0.9 * mm, stroke=0, fill=1)

    _draw_logo_mark(canvas, 18 * mm, A4[1] - 5.3 * mm)

    canvas.setFont("Helvetica", 8)
    canvas.setFillColor(colors.HexColor("#c7cad6"))
    canvas.drawRightString(A4[0] - 18 * mm, A4[1] - 11 * mm, "CAB QUALITY ENGINE")
    canvas.setFont("Helvetica-Bold", 9)
    canvas.setFillColor(colors.white)
    canvas.drawRightString(A4[0] - 18 * mm, A4[1] - 7 * mm, "Rapport d'analyse qualité")

    canvas.setFillColor(TEXT_MUTED)
    canvas.setFont("Helvetica", 7.5)
    canvas.drawCentredString(
        A4[0] / 2, 10 * mm,
        f"Document généré automatiquement le {datetime.now().strftime('%d/%m/%Y à %H:%M')} — "
        f"page {doc.page}",
    )

    canvas.setStrokeColor(BORDER)
    canvas.setLineWidth(0.6)
    canvas.line(18 * mm, 14 * mm, A4[0] - 18 * mm, 14 * mm)

    canvas.restoreState()


# ============================================================
# CONSTRUCTION DU RAPPORT
# ============================================================

def build_ticket_pdf(ticket):
    """ticket : dict (déjà préparé comme pour ticket.html — preuves et
    recommandations déjà désérialisées en listes Python)."""

    buffer = BytesIO()

    doc = BaseDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=18 * mm,
        rightMargin=18 * mm,
        topMargin=23 * mm,
        bottomMargin=18 * mm,
        title=f"Rapport CAB — {ticket.get('Clé de ticket', '')}",
    )

    frame = Frame(doc.leftMargin, doc.bottomMargin, doc.width, doc.height, id="normal")
    doc.addPageTemplates([PageTemplate(id="report", frames=[frame], onPage=_header_footer)])

    story = []

    key = _txt(ticket.get("Clé de ticket"))
    score = ticket.get("score_qualite_global")
    score_color = _score_color(score)
    level_color = _level_color(ticket.get("niveau_qualite"))

    # --------------------------------------------------------
    # EN-TETE
    # --------------------------------------------------------

    story.append(Paragraph("TICKET CAB — CHANGE ADVISORY BOARD", STYLES["eyebrow"]))
    story.append(Paragraph(f"{key}", STYLES["report_title"]))
    story.append(Paragraph(_txt(ticket.get("Résumé")), STYLES["report_subtitle"]))
    story.append(Spacer(1, 18))

    # --------------------------------------------------------
    # CARTE SCORE / NIVEAU — v2, design sobre.
    # Un seul bloc à fond neutre unique (SURFACE_MUTED), séparateur
    # vertical fin entre les deux valeurs. Seuls le chiffre du score et
    # le texte du niveau portent une couleur ; jamais le fond de la
    # carte, ni de bordure colorée épaisse : évite l'effet "gadget" de
    # la version précédente (deux fonds pastel + bordures colorées).
    # --------------------------------------------------------

    score_value_style = ParagraphStyle(
        "score_value", parent=_base["Normal"],
        fontName="Helvetica-Bold", fontSize=30, leading=34,
        textColor=score_color, alignment=TA_CENTER,
    )
    score_suffix_style = ParagraphStyle(
        "score_suffix", parent=_base["Normal"],
        fontName="Helvetica", fontSize=10, leading=13,
        textColor=TEXT_MUTED, alignment=TA_CENTER,
    )
    level_value_style = ParagraphStyle(
        "level_value", parent=_base["Normal"],
        fontName="Helvetica-Bold", fontSize=16, leading=20,
        textColor=level_color, alignment=TA_CENTER,
    )

    kpi_table = Table(
        [[
            [
                Paragraph("SCORE GLOBAL", STYLES["label"]),
                Spacer(1, 8),
                Paragraph(_num(score), score_value_style),
                Spacer(1, 2),
                Paragraph("sur 100 points", score_suffix_style),
            ],
            "",
            [
                Paragraph("NIVEAU DE QUALITÉ", STYLES["label"]),
                Spacer(1, 8),
                Spacer(1, 6),
                Paragraph(_txt(ticket.get("niveau_qualite")).upper(), level_value_style),
                Spacer(1, 2),
                Paragraph("évaluation globale", score_suffix_style),
            ],
        ]],
        colWidths=[doc.width * 0.465, doc.width * 0.07, doc.width * 0.465],
    )

    kpi_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), SURFACE_MUTED),
        ("BOX", (0, 0), (-1, -1), 0.7, BORDER),
        ("LINEAFTER", (0, 0), (0, 0), 0.7, BORDER),
        ("TOPPADDING", (0, 0), (-1, -1), 18),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 18),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
    ]))
    story.append(kpi_table)

    # --------------------------------------------------------
    # DESCRIPTION / IMPACT
    # --------------------------------------------------------

    description = _txt(ticket.get("Description"), fallback="")
    if description:
        story.append(Paragraph("Description", STYLES["section_heading"]))
        story.append(HRFlowable(width="100%", thickness=0.6, color=BORDER, spaceAfter=8))
        story.append(Paragraph(description.replace("\n", " "), STYLES["body"]))

    impact_text = _txt(ticket.get("Impact de l'action"), fallback="")
    if impact_text:
        story.append(Paragraph("Impact", STYLES["section_heading"]))
        story.append(HRFlowable(width="100%", thickness=0.6, color=BORDER, spaceAfter=8))
        story.append(Paragraph(impact_text.replace("\n", " "), STYLES["body"]))

    source_desc = ticket.get("source_description")
    if source_desc == "parent":
        story.append(Spacer(1, 5))
        story.append(Paragraph(
            "⚠ Ce ticket n'a pas de description propre : la description affichée "
            "ci-dessus provient de son ticket parent.",
            STYLES["body_muted"],
        ))

    # --------------------------------------------------------
    # ANALYSE
    # --------------------------------------------------------

    story.append(Paragraph("Analyse", STYLES["section_heading"]))
    story.append(HRFlowable(width="100%", thickness=0.6, color=BORDER, spaceAfter=8))
    story.append(Paragraph(
        _txt(ticket.get("analyse"), fallback="Analyse non disponible pour ce ticket.").replace("\n", " "),
        STYLES["body"],
    ))

    # --------------------------------------------------------
    # DETAIL DU SCORE /100 — barème officiel à jour
    # Complétude /25, Format /15, Pertinence /25, Impact /15, Cohérence /20
    # --------------------------------------------------------

    story.append(Paragraph("Détail du score /100", STYLES["section_heading"]))
    story.append(HRFlowable(width="100%", thickness=0.6, color=BORDER, spaceAfter=8))

    categories = [
        ("Complétude", ticket.get("score_completude"), 25),
        ("Format", ticket.get("score_format"), 15),
        ("Pertinence", ticket.get("score_pertinence"), 25),
        ("Impact", ticket.get("score_impact"), 15),
        ("Cohérence", ticket.get("score_coherence"), 20),
    ]

    score_rows = [["Catégorie", "Score obtenu", "Barème"]]
    for label, value, maximum in categories:
        score_rows.append([label, _num(value, "0"), f"/{maximum}"])
    score_rows.append(["Total", f"{_num(score, '0')}/100", ""])

    score_table = Table(score_rows, colWidths=[doc.width * 0.5, doc.width * 0.25, doc.width * 0.25])
    score_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), NAVY),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
        ("BACKGROUND", (0, -1), (-1, -1), ACCENT_SOFT),
        ("TEXTCOLOR", (0, -1), (-1, -1), ACCENT_DARK),
        ("FONTSIZE", (0, 0), (-1, -1), 9.5),
        ("GRID", (0, 0), (-1, -1), 0.5, BORDER),
        ("TOPPADDING", (0, 0), (-1, -1), 8),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
        ("ALIGN", (1, 0), (-1, -1), "CENTER"),
    ]))
    story.append(score_table)

    # --------------------------------------------------------
    # EXPLICATIONS
    # --------------------------------------------------------

    story.append(Paragraph("Explications détaillées", STYLES["section_heading"]))
    story.append(HRFlowable(width="100%", thickness=0.6, color=BORDER, spaceAfter=8))

    explications = [
        ("Complétude", ticket.get("explication_completude")),
        ("Format", ticket.get("explication_format")),
        ("Pertinence", ticket.get("explication_description")),
        ("Impact", ticket.get("explication_impact")),
        ("Cohérence", ticket.get("explication_coherence")),
    ]

    for label, text in explications:
        story.append(Paragraph(
            f'<font color="#cf4038"><b>{label}</b></font> — '
            f"{_txt(text, fallback='Non évalué.')}",
            STYLES["body"],
        ))
        story.append(Spacer(1, 7))

    # --------------------------------------------------------
    # RECOMMANDATIONS
    # --------------------------------------------------------

    story.append(Paragraph("Recommandations", STYLES["section_heading"]))
    story.append(HRFlowable(width="100%", thickness=0.6, color=BORDER, spaceAfter=8))
    story.append(_bullet_list(ticket.get("recommandations"), "Aucune recommandation pour ce ticket."))

    # --------------------------------------------------------
    # PREUVES
    # --------------------------------------------------------

    story.append(Paragraph("Preuves", STYLES["section_heading"]))
    story.append(HRFlowable(width="100%", thickness=0.6, color=BORDER, spaceAfter=8))
    story.append(_bullet_list(ticket.get("preuves"), "Aucune preuve disponible pour ce ticket."))

    doc.build(story)

    buffer.seek(0)
    return buffer

def _pct(count, total):
    return round(count / total * 100, 1) if total else 0.0


def _top_items(items_lists, n=6):
    """items_lists : liste de listes (ex: toutes les recommandations de
    tous les tickets filtrés). Retourne les n phrases les plus
    fréquentes."""
    flat = [str(item).strip() for lst in items_lists for item in (lst or []) if str(item).strip()]
    if not flat:
        return []
    counts = Counter(flat)
    return counts.most_common(n)


def _filtres_actifs_texte(filtres):
    """Traduit le dict de filtres actifs en une phrase lisible pour l'en-tête
    du rapport, ex: 'Niveau = Faible · Année = 2026'."""
    labels = {
        "search": "Recherche",
        "level": "Niveau",
        "status": "Statut",
        "year": "Année",
        "etat": "État Jira",
    }
    parts = []
    for key, label in labels.items():
        value = filtres.get(key)
        if value:
            value_affiche = "Sans date" if value == "__none__" else value
            parts.append(f"{label} = {value_affiche}")
    return " · ".join(parts) if parts else "Aucun filtre — portefeuille complet"


def _top_champs_manquants(champs_manquants_list, n=8):
    """champs_manquants_list : liste de chaînes "Description, Impact de
    l'action, Date de CAB" (une par ticket, format actuel de la colonne
    champs_manquants côté score_engine). Compte chaque champ INDIVIDUEL,
    pas la combinaison entière, pour répondre à "quels champs remplir
    en priorité sur ce sous-ensemble de tickets"."""
    counter = Counter()
    for champs_str in champs_manquants_list:
        if not champs_str or not str(champs_str).strip():
            continue
        for champ in str(champs_str).split(","):
            champ = champ.strip()
            if champ:
                counter[champ] += 1
    return counter.most_common(n)

def _is_number(v):
    try:
        float(v)
        return True
    except (TypeError, ValueError):
        return False


def _pct_of_max(value, maximum):
    return round(value / maximum * 100, 1) if maximum else 0.0


def _sous_scores_moyens(rows):
    """Moyenne de chaque sous-score sur les tickets filtrés, avec son
    barème /max associé -- répond à "quel axe (complétude, format,
    pertinence, impact, cohérence) est le plus faible sur ce sous-
    ensemble", indépendamment du niveau de filtre déjà appliqué."""
    axes = [
        ("score_completude", "Complétude", 25),
        ("score_format", "Format", 15),
        ("score_pertinence", "Pertinence", 25),
        ("score_impact", "Impact", 15),
        ("score_coherence", "Cohérence", 20),
    ]
    result = []
    for key, label, maximum in axes:
        valeurs = [r.get(key) for r in rows if r.get(key) is not None]
        valeurs = [float(v) for v in valeurs if _is_number(v)]
        moyenne = round(sum(valeurs) / len(valeurs), 1) if valeurs else 0.0
        result.append({"label": label, "moyenne": moyenne, "max": maximum, "pct": _pct_of_max(moyenne, maximum)})
    return result


def build_portfolio_pdf(rows, filtres, stats):
    """rows : liste de dicts (une entrée par ticket filtré, colonnes brutes
    du DataFrame -- pas le format déjà formaté pour le template HTML).
    filtres : dict {search, level, status, year, etat} tel que reçu de
    request.args au moment de l'appel.
    stats : dict {total, avg_score, excellent, good, medium, weak,
    hors_perimetre} déjà calculé par le dashboard (évite de recalculer)."""

    buffer = BytesIO()

    doc = BaseDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=18 * mm,
        rightMargin=18 * mm,
        topMargin=23 * mm,
        bottomMargin=18 * mm,
        title="Rapport de synthèse — Portefeuille CAB",
    )

    frame = Frame(doc.leftMargin, doc.bottomMargin, doc.width, doc.height, id="normal")
    doc.addPageTemplates([PageTemplate(id="report", frames=[frame], onPage=_header_footer)])

    story = []

    total = stats.get("total", 0) or 0
    avg_score = stats.get("avg_score", 0) or 0
    filtre_niveau_actif = bool(filtres.get("level"))

    # --------------------------------------------------------
    # EN-TETE
    # --------------------------------------------------------

    story.append(Paragraph("RAPPORT DE SYNTHÈSE — PORTEFEUILLE CAB", STYLES["eyebrow"]))
    story.append(Paragraph("Analyse des tickets affichés", STYLES["report_title"]))
    story.append(Paragraph(_filtres_actifs_texte(filtres), STYLES["report_subtitle"]))
    story.append(Spacer(1, 4))
    story.append(Paragraph(
        f"{total} ticket(s) inclus dans cette synthèse.",
        STYLES["body_muted"],
    ))
    story.append(Spacer(1, 18))

    # --------------------------------------------------------
    # CARTE SCORE MOYEN / NIVEAU DOMINANT
    # --------------------------------------------------------

    score_color = _score_color(avg_score)

    niveau_counts = {
        "Excellent": stats.get("excellent", 0) or 0,
        "Bon": stats.get("good", 0) or 0,
        "Moyen": stats.get("medium", 0) or 0,
        "Faible": stats.get("weak", 0) or 0,
    }
    niveau_dominant = max(niveau_counts, key=niveau_counts.get) if total else "—"
    level_color = _level_color(niveau_dominant)

    score_value_style = ParagraphStyle(
        "score_value_pf", parent=_base["Normal"],
        fontName="Helvetica-Bold", fontSize=30, leading=34,
        textColor=score_color, alignment=TA_CENTER,
    )
    score_suffix_style = ParagraphStyle(
        "score_suffix_pf", parent=_base["Normal"],
        fontName="Helvetica", fontSize=10, leading=13,
        textColor=TEXT_MUTED, alignment=TA_CENTER,
    )
    level_value_style = ParagraphStyle(
        "level_value_pf", parent=_base["Normal"],
        fontName="Helvetica-Bold", fontSize=16, leading=20,
        textColor=level_color, alignment=TA_CENTER,
    )

    kpi_table = Table(
        [[
            [
                Paragraph("SCORE MOYEN", STYLES["label"]),
                Spacer(1, 8),
                Paragraph(_num(avg_score), score_value_style),
                Spacer(1, 2),
                Paragraph("sur 100 points", score_suffix_style),
            ],
            "",
            [
                Paragraph("NIVEAU DOMINANT", STYLES["label"]),
                Spacer(1, 8),
                Spacer(1, 6),
                Paragraph(niveau_dominant.upper(), level_value_style),
                Spacer(1, 2),
                Paragraph(f"{_pct(niveau_counts.get(niveau_dominant, 0), total)}% des tickets", score_suffix_style),
            ],
        ]],
        colWidths=[doc.width * 0.465, doc.width * 0.07, doc.width * 0.465],
    )
    kpi_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), SURFACE_MUTED),
        ("BOX", (0, 0), (-1, -1), 0.7, BORDER),
        ("LINEAFTER", (0, 0), (0, 0), 0.7, BORDER),
        ("TOPPADDING", (0, 0), (-1, -1), 18),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 18),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
    ]))
    story.append(kpi_table)

    # --------------------------------------------------------
    # REPARTITION PAR NIVEAU — masquée si le filtre "level" est déjà
    # actif (100% du sous-ensemble serait mécaniquement sur ce seul
    # niveau : information triviale, sans valeur ajoutée).
    # --------------------------------------------------------

    if not filtre_niveau_actif:
        story.append(Paragraph("Répartition par niveau de qualité", STYLES["section_heading"]))
        story.append(HRFlowable(width="100%", thickness=0.6, color=BORDER, spaceAfter=8))

        repartition_rows = [["Niveau", "Nombre de tickets", "Pourcentage"]]
        for niveau, count in niveau_counts.items():
            repartition_rows.append([niveau, str(count), f"{_pct(count, total)}%"])
        if stats.get("hors_perimetre"):
            repartition_rows.append([
                "Hors périmètre", str(stats["hors_perimetre"]),
                f"{_pct(stats['hors_perimetre'], total + stats['hors_perimetre'])}%",
            ])

        repartition_table = Table(
            repartition_rows,
            colWidths=[doc.width * 0.45, doc.width * 0.30, doc.width * 0.25],
        )
        repartition_table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), NAVY),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, -1), 9.5),
            ("GRID", (0, 0), (-1, -1), 0.5, BORDER),
            ("TOPPADDING", (0, 0), (-1, -1), 7),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
            ("ALIGN", (1, 0), (-1, -1), "CENTER"),
        ]))
        story.append(repartition_table)

    # --------------------------------------------------------
    # SOUS-SCORES MOYENS (complétude/format/pertinence/impact/cohérence)
    # -- reste affiché même si le filtre niveau est actif : savoir quel
    # axe plombe le plus les tickets "Faible" est une info utile.
    # --------------------------------------------------------

    sous_scores = _sous_scores_moyens(rows)

    story.append(Paragraph("Moyenne des sous-scores", STYLES["section_heading"]))
    story.append(HRFlowable(width="100%", thickness=0.6, color=BORDER, spaceAfter=8))

    if rows:
        sous_scores_tries = sorted(sous_scores, key=lambda s: s["pct"])
        sous_score_rows = [["Axe", "Score moyen", "Barème", "% du barème"]]
        for s in sous_scores_tries:
            sous_score_rows.append([s["label"], str(s["moyenne"]), f"/{s['max']}", f"{s['pct']}%"])

        sous_score_table = Table(
            sous_score_rows,
            colWidths=[doc.width * 0.30, doc.width * 0.22, doc.width * 0.18, doc.width * 0.30],
        )

        table_style_cmds = [
            ("BACKGROUND", (0, 0), (-1, 0), NAVY),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, -1), 9.5),
            ("GRID", (0, 0), (-1, -1), 0.5, BORDER),
            ("TOPPADDING", (0, 0), (-1, -1), 7),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
            ("ALIGN", (1, 0), (-1, -1), "CENTER"),
        ]
        # L'axe le plus faible (première ligne après le tri croissant) est
        # mis en évidence en rouge.
        if len(sous_score_rows) > 1:
            table_style_cmds.append(("TEXTCOLOR", (0, 1), (-1, 1), DANGER))
            table_style_cmds.append(("FONTNAME", (0, 1), (-1, 1), "Helvetica-Bold"))

        sous_score_table.setStyle(TableStyle(table_style_cmds))
        story.append(sous_score_table)
        story.append(Spacer(1, 6))
        story.append(Paragraph(
            f"Axe le plus faible sur ce sous-ensemble : <b>{sous_scores_tries[0]['label']}</b> "
            f"({sous_scores_tries[0]['moyenne']}/{sous_scores_tries[0]['max']}, soit {sous_scores_tries[0]['pct']}% du barème).",
            STYLES["body_muted"],
        ))
    else:
        story.append(Paragraph("Aucun ticket pour calculer les sous-scores.", STYLES["body_muted"]))

    # --------------------------------------------------------
    # CHAMPS OBLIGATOIRES LES PLUS SOUVENT MANQUANTS
    # --------------------------------------------------------

    champs_manquants_bruts = [r.get("champs_manquants", "") for r in rows]
    top_champs = _top_champs_manquants(champs_manquants_bruts, n=8)

    story.append(Paragraph("Champs obligatoires les plus souvent manquants", STYLES["section_heading"]))
    story.append(HRFlowable(width="100%", thickness=0.6, color=BORDER, spaceAfter=8))

    if top_champs:
        champs_rows = [["Champ", "Nombre de tickets concernés", "% du sous-ensemble"]]
        for champ, count in top_champs:
            champs_rows.append([champ, str(count), f"{_pct(count, total)}%"])

        champs_table = Table(
            champs_rows,
            colWidths=[doc.width * 0.45, doc.width * 0.30, doc.width * 0.25],
        )
        champs_table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), NAVY),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, -1), 9.5),
            ("GRID", (0, 0), (-1, -1), 0.5, BORDER),
            ("TOPPADDING", (0, 0), (-1, -1), 7),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
            ("ALIGN", (1, 0), (-1, -1), "CENTER"),
        ]))
        story.append(champs_table)
    else:
        story.append(Paragraph(
            "Tous les champs obligatoires sont renseignés sur ce sous-ensemble.",
            STYLES["body_muted"],
        ))

    # --------------------------------------------------------
    # RECOMMANDATIONS LES PLUS FREQUENTES
    # --------------------------------------------------------

    all_recommandations = [r.get("recommandations") or [] for r in rows]
    top_recos = _top_items(all_recommandations, n=6)

    story.append(Paragraph("Recommandations les plus fréquentes", STYLES["section_heading"]))
    story.append(HRFlowable(width="100%", thickness=0.6, color=BORDER, spaceAfter=8))

    if top_recos:
        story.append(_bullet_list(
            [f"{text}  ({count} ticket{'s' if count > 1 else ''})" for text, count in top_recos],
            "Aucune recommandation disponible sur ce sous-ensemble.",
        ))
    else:
        story.append(Paragraph(
            "Aucune recommandation disponible sur ce sous-ensemble.",
            STYLES["body_muted"],
        ))

    # --------------------------------------------------------
    # LISTE DES TICKETS INCLUS DANS CE RAPPORT
    # --------------------------------------------------------

    story.append(Paragraph(f"Détail des {total} ticket(s) inclus", STYLES["section_heading"]))
    story.append(HRFlowable(width="100%", thickness=0.6, color=BORDER, spaceAfter=8))

    if rows:
        ticket_rows = [["Ticket", "Résumé", "Score", "Niveau", "État Jira"]]
        for r in rows:
            resume = str(r.get("Résumé", "") or "—")
            resume_court = resume[:55] + ("…" if len(resume) > 55 else "")
            ticket_rows.append([
                str(r.get("Clé de ticket", "—")),
                resume_court,
                _num(r.get("score_qualite_global"), "—"),
                str(r.get("niveau_qualite", "—")),
                str(r.get("etat_jira", "—") or "—"),
            ])

        ticket_table = Table(
            ticket_rows,
            colWidths=[doc.width * 0.14, doc.width * 0.46, doc.width * 0.12, doc.width * 0.14, doc.width * 0.14],
            repeatRows=1,
        )
        ticket_table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), NAVY),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, -1), 8),
            ("GRID", (0, 0), (-1, -1), 0.4, BORDER),
            ("TOPPADDING", (0, 0), (-1, -1), 5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ("ALIGN", (2, 0), (2, -1), "CENTER"),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ]))
        story.append(ticket_table)
    else:
        story.append(Paragraph(
            "Aucun ticket ne correspond aux filtres appliqués.",
            STYLES["body_muted"],
        ))

    doc.build(story)

    buffer.seek(0)
    return buffer