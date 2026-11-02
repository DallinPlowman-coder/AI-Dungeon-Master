"""One source of truth for player downloads and explicit DM-only exports."""
import io
import json
from html import escape
from pathlib import Path

from .records import ABILITIES, CharacterSheet


def signed(value):
    return f"{value:+d}"


def markdown_document(campaign, kind, dm=False):
    title = {"character":"Character sheet", "log":"Campaign log", "relationships":"NPC relationships",
             "plan":"DM campaign plan"}[kind]
    lines = [f"# {title}", "", f"Campaign: {campaign['name']}", "",
             "DM ONLY — contains spoilers" if dm else "Player copy", ""]
    if kind == "character":
        sheet = CharacterSheet.model_validate(campaign["character"])
        stats, derived = sheet.model_dump(), sheet.derived()
        lines += [f"## {sheet.name or 'Unnamed character'}", ""]
        lines += [f"Player: {sheet.player_name or '—'} · Pronouns: {sheet.pronouns or '—'}", "",
                  f"Ancestry: {sheet.ancestry or '—'} · Background: {sheet.background or '—'}", "",
                  f"Class: {sheet.class_name or '—'} · Subclass: {sheet.subclass or '—'} · Level: {sheet.level}", "",
                  f"Alignment: {sheet.alignment or '—'} · Experience: {sheet.experience}", ""]
        lines += ["## Ability scores", "", "| Ability | Score | Modifier | Saving throw |", "| --- | --- | --- | --- |"]
        for key in ABILITIES:
            lines += [f"| {key.title()} | {stats['abilities'][key]} | {signed(derived['modifiers'][key])} | {signed(derived['saving_throws'][key])} |"]
        lines += ["", "## Combat and resources", "",
                  f"HP: {sheet.hp_current} / {sheet.hp_max} · Temporary HP: {sheet.temp_hp}", "",
                  f"Armor class: {sheet.armor_class} · Initiative: {signed(derived['initiative'])} · Speed: {sheet.speed} ft", "",
                  f"Proficiency bonus: {signed(sheet.proficiency_bonus)} · Passive perception: {derived['passive_perception']}", "",
                  f"Hit dice: {sheet.hit_dice or '—'} · Inspiration: {'Yes' if sheet.inspiration else 'No'}", "",
                  f"Death saves: {sheet.death_save_successes} successes / {sheet.death_save_failures} failures", "",
                  f"Conditions: {', '.join(sheet.conditions) or 'None recorded'}", "",
                  "## Skills", "", "| Skill | Bonus | Training |", "| --- | --- | --- |"]
        for skill, bonus in derived["skills"].items():
            training = "Expertise" if skill in sheet.skill_expertise else "Proficient" if skill in sheet.skill_proficiencies else "—"
            lines += [f"| {skill} | {signed(bonus)} | {training} |"]
        lines += ["", "## Spellcasting", "",
                  f"Ability: {sheet.spellcasting_ability or 'None recorded'} · Save DC: {derived['spell_save_dc'] or '—'} · Attack bonus: {signed(derived['spell_attack_bonus']) if derived['spell_attack_bonus'] is not None else '—'}", ""]
        for slot in sheet.spell_slots:
            lines += [f"Level {slot.level}: {slot.total - slot.used} / {slot.total} slots remaining", ""]
        for key in ("attacks", "inventory", "currency", "features", "languages", "spells", "appearance", "backstory", "personality", "ideals", "bonds", "flaws", "goals", "notes"):
            if stats[key]:
                lines += [f"## {key.replace('_', ' ').title()}", "", stats[key], ""]
        if dm:
            lines += ["## Private character notes", "", stats["dm_notes"] or "Nothing recorded yet.", ""]
    elif kind == "log":
        for event in campaign["events"]:
            lines += [f"## Session {event['session']} · {event['title']}", "",
                      f"{event['created_at']} · {event['kind']} · {event['world_time'] or 'World time not set'}", "",
                      event["summary"], ""]
            if event["details"]:
                lines += [event["details"], ""]
            lines += [f"Event ID: {event['id']}", ""]
        if not campaign["events"]:
            lines += ["No story events have been recorded yet.", ""]
    elif kind == "relationships":
        for npc in campaign["npcs"]:
            lines += [f"## {npc['name']}", "", npc["role"] or "Role not recorded.", "", npc["public_notes"] or "No public notes.", ""]
            if "disposition" in npc:
                lines += [f"Disposition: {npc['disposition']} · Trust: {npc['trust']}", "", npc.get("feelings") or "", ""]
            else:
                lines += ["Their private feelings have not been revealed.", ""]
            lines += ["### Known information about the player", ""]
            for fact in npc["knowledge"]:
                lines += [f"- {fact['fact']} — Learned from: {fact['learned_from']}"]
                if fact.get("evidence_event_id"):
                    lines += [f"  Evidence event: {fact['evidence_event_id']}"]
            if not npc["knowledge"]:
                lines += ["No revealed knowledge recorded."]
            lines += [""]
            if dm:
                lines += ["### Private motives and notes", "", npc.get("private_notes") or "Nothing recorded.", ""]
        if not campaign["npcs"]:
            lines += ["No known NPCs recorded yet.", ""]
    else:
        plan = campaign.get("plan")
        if not plan:
            raise ValueError("The campaign plan is not ready")
        lines += [f"Source adventure: {plan['source_book']['title']} · Plan version {plan['version']}", "",
                  f"Read {plan['coverage']['passages_read']} of {plan['coverage']['total_passages']} indexed passages.", "",
                  plan.get("generation_notice", "AI-generated roadmap. Verify established facts against the source book; branches and endings are possibilities."), "",
                  plan["overview"], ""]
        for phase in plan["phases"]:
            lines += [f"## {phase['title']} · {phase['status']}", "", phase["summary"], "",
                      f"Goal: {phase['goal']}", "", "### Possible scenes", ""]
            lines += [f"- {scene}" for scene in phase["scenes"]] + ["", "### Branches and consequences", ""]
            lines += [f"- {branch}" for branch in phase["branches"]] + ["", f"Completion: {phase['completion_condition']}", ""]
            lines += ["Sources: " + ", ".join(f"[{source}] {plan['sources'][source]}" for source in phase["source_ids"]), ""]
        for key in ("endings", "npc_agendas", "unresolved_questions"):
            lines += [f"## {key.replace('_', ' ').title()}", ""]
            lines += [f"- {entry}" for entry in plan[key]] + [""]
    return "\n".join(lines)


def pdf_document(markdown):
    from reportlab import __file__ as reportlab_file
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_LEFT
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    fonts = Path(reportlab_file).parent / "fonts"
    if "Archive" not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont("Archive", str(fonts / "Vera.ttf")))
        pdfmetrics.registerFont(TTFont("ArchiveBold", str(fonts / "VeraBd.ttf")))
    styles = getSampleStyleSheet()
    for name in ("Normal", "Heading1", "Heading2", "Heading3"):
        styles[name].fontName = "ArchiveBold" if name.startswith("Heading") else "Archive"
        styles[name].textColor = colors.HexColor("#35212e")
    styles["Normal"].fontSize = 9
    styles["Normal"].leading = 14
    styles["Normal"].spaceAfter = 7
    styles["Heading1"].fontSize = 23
    styles["Heading1"].leading = 29
    styles["Heading2"].fontSize = 14
    styles["Heading2"].leading = 19
    cell_style = ParagraphStyle("Cell", parent=styles["Normal"], fontSize=8, leading=12, spaceAfter=0, alignment=TA_LEFT)
    output = io.BytesIO()
    document = SimpleDocTemplate(output, pagesize=A4, rightMargin=42, leftMargin=42, topMargin=45, bottomMargin=45,
                                 title=markdown.splitlines()[0].lstrip("# "), author="AI Dungeon Master")
    story, lines, index = [], markdown.splitlines(), 0
    while index < len(lines):
        line = lines[index]
        if line == "## Skills" and markdown.startswith("# Character sheet\n"):
            story.append(PageBreak())
        if line.startswith("|") and index + 1 < len(lines) and lines[index + 1].startswith("| ---"):
            rows = []
            while index < len(lines) and lines[index].startswith("|"):
                if not lines[index].startswith("| ---"):
                    rows.append([Paragraph(escape(cell.strip()), cell_style) for cell in lines[index].strip("|").split("|")])
                index += 1
            table = Table(rows, colWidths=[document.width / len(rows[0])] * len(rows[0]), repeatRows=1, hAlign="LEFT")
            table.setStyle(TableStyle([("BACKGROUND", (0,0), (-1,0), colors.HexColor("#e6d5df")),
                                       ("ROWBACKGROUNDS", (0,1), (-1,-1), [colors.HexColor("#faf7f9"), colors.white]),
                                       ("VALIGN", (0,0), (-1,-1), "TOP"),
                                       ("TOPPADDING", (0,0), (-1,-1), 5), ("BOTTOMPADDING", (0,0), (-1,-1), 5),
                                       ("LINEBELOW", (0,0), (-1,0), .5, colors.HexColor("#b488a0"))]))
            story.extend([table, Spacer(1,12)])
            continue
        if not line.strip():
            spacer = Spacer(1,4)
            if story and getattr(story[-1], "getKeepWithNext", lambda: False)():
                spacer.keepWithNext = True
            story.append(spacer)
        else:
            heading = len(line) - len(line.lstrip("#"))
            style = styles[f"Heading{min(heading,3)}"] if heading and line[heading:heading+1] == " " else styles["Normal"]
            text = line[heading:].strip() if style is not styles["Normal"] else line
            story.append(Paragraph(escape(text), style))
        index += 1

    def footer(canvas, doc):
        canvas.setFont("Archive", 7)
        canvas.setFillColor(colors.HexColor("#887080"))
        canvas.drawString(42, 25, "AI DUNGEON MASTER · Campaign records")
        canvas.drawRightString(A4[0] - 42, 25, str(doc.page))
    document.build(story, onFirstPage=footer, onLaterPages=footer)
    return output.getvalue()


def json_document(campaign, kind):
    data = {"campaign":campaign["name"], "character":campaign["character"], "derived":campaign["derived"]} if kind == "character" else {
        "campaign":campaign["name"], kind:campaign[{"log":"events", "relationships":"npcs", "plan":"plan"}[kind]]}
    return json.dumps(data, ensure_ascii=False, indent=2).encode()
