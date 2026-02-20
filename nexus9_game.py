#!/usr/bin/env python3
"""NEXUS-9 AI Game Master — Textual TUI
Features: bilingual (EN/FR), save/load, 7-inch layout, TTS, Setup Wizard
"""

import json, os, re, random, subprocess, tempfile, base64, wave, threading
import concurrent.futures
from pathlib import Path
from datetime import datetime

from textual.app     import App, ComposeResult
from textual.containers import Container
from textual.widgets import Static, Input, RichLog
from textual.screen  import Screen
from textual.binding import Binding
from rich.text import Text
import requests
from google import genai
from google.genai import types

try:
    import anthropic as _anthropic
    ANTHROPIC_AVAILABLE = True
except ImportError:
    ANTHROPIC_AVAILABLE = False

try:
    import openai as _openai
    OPENAI_AVAILABLE = True
except ImportError:
    OPENAI_AVAILABLE = False

# ============================================================
# CONFIG
# ============================================================
SCRIPT_DIR = Path(__file__).resolve().parent

SFX_MAP = {
    "BANG": "Bang!", "CLANG": "Clang!", "THUD": "Thud!",
    "BOOM": "Boom!", "CRACK": "Crack!", "CLICK": "Click!", "BEEP": "Bip!",
}
PRONOUNCE_MAP = {"VANTA": "Vanta", "KITE-SHIELD": "Kite Shield"}

TTS_POOLS = {
    "fr": {
        "male":   ["fr-CA-Neural2-B", "fr-FR-Neural2-B", "fr-FR-Wavenet-D"],
        "female": ["fr-CA-Neural2-A", "fr-FR-Neural2-A", "fr-FR-Wavenet-C"],
        "neutral":["fr-CA-Neural2-B"],
    },
    "en": {
        "male":   ["en-US-Neural2-A", "en-US-Neural2-D", "en-US-Wavenet-D"],
        "female": ["en-US-Neural2-C", "en-US-Neural2-F", "en-US-Wavenet-G"],
        "neutral":["en-US-Neural2-J"],
    }
}

EDGE_VOICES = {
    "fr": {
        "male":   ["fr-CA-AntoineNeural", "fr-FR-HenriNeural"],
        "female": ["fr-CA-SylvieNeural", "fr-FR-DeniseNeural"],
        "neutral":["fr-CA-AntoineNeural"]
    },
    "en": {
        "male":   ["en-US-GuyNeural", "en-US-ChristopherNeural"],
        "female": ["en-US-AriaNeural", "en-US-JennyNeural"],
        "neutral":["en-US-GuyNeural"]
    }
}

EMOTION_PROFILES = {
    "neutral":   {"rate": 1.00, "pitch":  0.0, "emphasis": "none"},
    "angry":     {"rate": 1.20, "pitch":  4.0, "emphasis": "strong"},
    "menacing":  {"rate": 0.78, "pitch": -3.5, "emphasis": "moderate"},
    "nervous":   {"rate": 1.30, "pitch":  2.5, "emphasis": "none"},
    "desperate": {"rate": 1.18, "pitch":  2.0, "emphasis": "strong"},
    "cold":      {"rate": 0.85, "pitch": -2.5, "emphasis": "none"},
    "excited":   {"rate": 1.30, "pitch":  4.5, "emphasis": "strong"},
    "sad":       {"rate": 0.78, "pitch": -3.0, "emphasis": "none"},
    "sarcastic": {"rate": 1.08, "pitch":  2.5, "emphasis": "moderate"},
    "warning":   {"rate": 0.88, "pitch": -1.5, "emphasis": "strong"},
}

GENDER_RATE  = {"male": 0.96, "female": 1.04, "neutral": 1.00}
GENDER_PITCH = {"male": -2.5, "female": 2.5,  "neutral": 0.0}
AGE_PITCH_OFFSET = {"child": 5.0, "teen": 2.0, "adult": 0.0, "elder": -3.5}
AGE_RATE_MOD     = {"child": 1.10, "teen": 1.04, "adult": 1.0, "elder": 0.88}
AGE_KEYWORDS = {"child": "child", "kid": "child", "young": "teen", "teen": "teen", "adult": "adult", "old": "elder", "elder": "elder"}

KEYWORD_EMOTIONS = {
    "angry":     ["kill", "destroy", "hate", "furious", "damn", "idiot"],
    "nervous":   ["please", "i can't", "wait", "no no", "hurry", "scared", "afraid"],
    "menacing":  ["you're dead", "find you", "end you", "disappear", "regret"],
    "excited":   ["yes!", "finally", "let's go", "amazing", "can't believe"],
    "sad":       ["sorry", "miss", "gone", "lost", "alone", "can't do this"],
    "desperate": ["help me", "need", "dying", "last chance", "begging", "no time"],
    "cold":      ["irrelevant", "don't care", "whatever", "move along", "not my problem"],
    "sarcastic": ["oh really", "how nice", "wonderful", "how lovely", "of course"],
    "warning":   ["careful", "watch out", "get out", "run", "danger", "behind you", "they're coming"],
}

def _infer_emotion(text):
    t = text.lower()
    for emotion, keywords in KEYWORD_EMOTIONS.items():
        if any(kw in t for kw in keywords): return emotion
    return "neutral"

def _stable_pick(name: str, pool: list[str]) -> str:
    if not pool: return ""
    h = 0
    for ch in name: h = (h * 131 + ord(ch)) & 0xFFFFFFFF
    return pool[h % len(pool)]

def _normalize_for_tts(text: str) -> str:
    t = str(text)
    def repl_bracket(m): return SFX_MAP.get(m.group(1), m.group(1).title() + "!")
    t = re.sub(r'\[(BANG|CLANG|THUD|BOOM|CRACK|CLICK|BEEP)\]', repl_bracket, t)
    t = re.sub(r'\s+', ' ', t).strip()
    return t

def load_env():
    env = {}
    p = SCRIPT_DIR / ".env"
    if p.exists():
        for line in p.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    return env

def save_env(updates):
    current = load_env()
    current.update(updates)
    p = SCRIPT_DIR / ".env"
    with open(p, "w") as f:
        for k, v in current.items(): f.write(f"{k}={v}\n")
    ENV.update(current)

ENV = load_env()
RULES_PATH     = Path(ENV.get("RULES_PATH",  str(SCRIPT_DIR / "nexus9_rules.txt")))
WORLD_PATH     = Path(ENV.get("WORLD_PATH",  str(SCRIPT_DIR / "nexus9_world.txt")))
SAVE_DIR       = SCRIPT_DIR / "saves"
SPEAKER_DEVICE = ENV.get("SPEAKER_DEVICE", "plughw:0,0")
SAMPLE_RATE    = 24000

# ============================================================
# I18N
# ============================================================
STRINGS = {
    "en": {
        "cc_name":             "Enter your character's name:",
        "cc_playbook":         "=== CHOOSE PLAYBOOK ===",
        "cc_bg":               "=== CHOOSE BACKGROUND ===",
        "cc_bio_title":       "=== BIO ===",
        "cc_bio_sub":         "Write 1–3 sentences describing your character (look, vibe, motivation).",
        "cc_attrpkg":          "=== ATTRIBUTE PACKAGE ===",
        "cc_assign":           "=== ASSIGN ATTRIBUTES ===",
        "cc_assign_q":         "Assign d{die} to which attribute?",
        "cc_sk10_title":       "=== SKILLS: d10 (pick 2) ===",
        "cc_sk10_sub":         "Your two strongest skills:",
        "cc_sk8_title":        "=== SKILLS: d8 (pick 3) ===",
        "cc_sk8_sub":          "Three solid skills:",
        "cc_sk6_title":        "=== SKILLS: d6 (pick 3) ===",
        "cc_sk6_sub":          "Three backup skills:",
        "cc_pb_boost_title":   "=== PLAYBOOK BOOST ===",
        "cc_pb_boost_skills":  "Skill boosts (applied automatically):",
        "cc_pb_boost_attr":    "Choose attribute boost:",
        "cc_boosts_title":     "=== BOOSTS APPLIED ===",
        "cc_boosts_continue":  "Press Enter to continue",
        "cc_anchor":           "=== ANCHOR ===",
        "cc_anchor_sub":       "Who or what can you NOT abandon?\n(person, place, debt, promise, secret)",
        "cc_scar":             "=== SCAR ===",
        "cc_scar_sub":         "What did the city already take?\n(damaged ID, blacklist, missing someone,\n flawed augment, under surveillance, survived a system wipe)",
        "cc_summary":          "=== CHARACTER SUMMARY ===",
        "cc_begin":            "Press Enter to begin campaign:",
        "cc_enter_number":     "Enter number:",
        "cc_enter_two":        "Enter two numbers (e.g. 1,3):",
        "cc_enter_three":      "Enter three numbers (e.g. 1,2,4):",
        "sl_title":            "=== SAVE FILES ===",
        "sl_prompt":           "Enter number to load, or 'n' for new:",
        "processing":          "Processing...",
        "launching":           "Starting campaign...",
        "gm_connected":        "GM connected",
        "resumed":             "Campaign resumed.",
        "saved":               "Saved: ",
        "loaded":              "Loaded: ",
        "nothing_to_save":     "Nothing to save yet.",
        "no_campaign":         "No active campaign.",
        "help_title":          "-- Commands --",
        "help_save":           "  Ctrl+S     Save game",
        "help_mute":           "  Ctrl+M      Mute/unmute TTS",
        "help_stats":          "  vit/chg/arm/marks/heat/creds +/-N",
        "help_sheet":          "  sheet        Show character sheet",
        "label_skills":        "Skills:",
        "label_attr":          "Attr:",
        "label_edge":          "Edge:",
        "label_plus1each":     "+1 each",
        "or_word":             " or ",
        "label_boosts":        "Boosts:",
        "label_pool":          "Pool:",
        "cc_pb_grants":        "grants:",
        "label_anchor":        "Anchor:",
        "label_scar":          "Scar:",
        "sheet_title":         "=== CHARACTER SHEET ===",
        "sheet_attributes":    "Attributes:",
        "sheet_skills":        "Skills:",
        "cc_credits_title":    "=== STARTING CREDS ===",
        "cc_gear_title":       "=== PICK GEAR ===",
        "cc_gear_picked":      "Picked:",
        "cc_gear_none":        "(none)",
        "cc_gear_prompt":      "Type a number to add gear, or 'done':",
        "cc_gear_cant_afford": "Not enough Creds.",
        "cc_gear_dup_armor":   "Already equipped. Pick something else.",
    },
    "fr": {
        "cc_name":             "Entrez le nom de votre personnage :",
        "cc_playbook":         "=== CHOISIR UN PLAYBOOK ===",
        "cc_bg":               "=== CHOISIR UNE ORIGINE ===",
        "cc_bio_title":       "=== BIO ===",
        "cc_bio_sub":         "Écris 1–3 phrases décrivant ton personnage (look, vibe, motivation).",
        "cc_attrpkg":          "=== PACKAGE D'ATTRIBUTS ===",
        "cc_assign":           "=== ASSIGNER LES ATTRIBUTS ===",
        "cc_assign_q":         "Assigner le d{die} a quel attribut ?",
        "cc_sk10_title":       "=== COMPÉTENCES : d10 (choisir 2) ===",
        "cc_sk10_sub":         "Vos deux compétences les plus fortes :",
        "cc_sk8_title":        "=== COMPÉTENCES : d8 (choisir 3) ===",
        "cc_sk8_sub":          "Trois compétences solides :",
        "cc_sk6_title":        "=== COMPÉTENCES : d6 (choisir 3) ===",
        "cc_sk6_sub":          "Trois compétences de secours :",
        "cc_pb_boost_title":   "=== BOOST DU PLAYBOOK ===",
        "cc_pb_boost_skills":  "Bonifications de compétences (appliquées automatiquement) :",
        "cc_pb_boost_attr":    "Choisir le boost d'attribut :",
        "cc_boosts_title":     "=== BOOSTS APPLIQUÉS ===",
        "cc_boosts_continue":  "Appuyez sur Entrée pour continuer",
        "cc_anchor":           "=== ANCRE ===",
        "cc_anchor_sub":       "Qui ou quoi ne pouvez-vous PAS abandonner ?\n(personne, lieu, dette, promesse, secret)",
        "cc_scar":             "=== CICATRICE ===",
        "cc_scar_sub":         "Qu'est-ce que la ville vous a déjà pris ?\n(ID endommagé, liste noire, quelqu'un de disparu,\n augment défaillant, surveillé, survécu à un wipe)",
        "cc_summary":          "=== RÉSUMÉ DU PERSONNAGE ===",
        "cc_begin":            "Appuyez sur Entrée pour lancer la campagne :",
        "cc_enter_number":     "Entrez un numéro :",
        "cc_enter_two":        "Entrez deux numéros (ex : 1,3) :",
        "cc_enter_three":      "Entrez trois numéros (ex : 1,2,4) :",
        "sl_title":            "=== FICHIERS SAUVEGARDÉS ===",
        "sl_prompt":           "Entrez un numéro pour charger, ou 'n' pour nouveau :",
        "processing":          "Traitement...",
        "launching":           "Démarrage de la campagne...",
        "gm_connected":        "GM connecté",
        "resumed":             "Campagne reprise.",
        "saved":               "Sauvegarde : ",
        "loaded":              "Charge : ",
        "nothing_to_save":     "Rien a sauvegarder.",
        "no_campaign":         "Pas de campagne active.",
        "help_title":          "-- Commandes --",
        "help_save":           "  Ctrl+S     Sauvegarder",
        "help_mute":           "  Ctrl+M      Activer/desactiver TTS",
        "help_stats":          "  vit/chg/arm/marks/heat/creds +/-N",
        "help_sheet":          "  sheet        Afficher la feuille de personnage",
        "label_skills":        "Compétences :",
        "label_attr":          "Attributs :",
        "label_edge":          "Edge :",
        "label_plus1each":     "+1 chacun",
        "or_word":             " ou ",
        "label_boosts":        "Bonifications :",
        "label_pool":          "Parc :",
        "cc_pb_grants":        "offre :",
        "label_anchor":        "Ancre :",
        "label_scar":          "Cicatrice :",
        "sheet_title":         "=== FICHE DE PERSONNAGE ===",
        "sheet_attributes":    "Attributs :",
        "sheet_skills":        "Compétences :",
        "cc_credits_title":    "=== CREDS DE DÉPART ===",
        "cc_gear_title":       "=== CHOISIR SON ÉQUIPEMENT ===",
        "cc_gear_picked":      "Choisi :",
        "cc_gear_none":        "(rien)",
        "cc_gear_prompt":      "Tapez un numéro pour ajouter de l'équipement, ou 'done' :",
        "cc_gear_cant_afford": "Pas assez de Creds.",
        "cc_gear_dup_armor":   "Déjà équipé. Choisissez autre chose.",
    }
}

TTS_VOICES = {
    "en": {"female":"en-US-Neural2-C", "male":"en-US-Neural2-D", "neutral":"en-US-Neural2-J", "narrator":"en-US-Neural2-J", "lang":"en-US"},
    "fr": {"female":"fr-CA-Neural2-A", "male":"fr-CA-Neural2-B", "neutral":"fr-CA-Neural2-B", "narrator":"fr-CA-Neural2-D", "lang":"fr-CA"},
}

PLAYBOOKS = {
    "Riotline":     {"desc":{"en":"Front-line brawler",      "fr":"Cogneur de première ligne"},  "skills":["Fight","Move"],    "attr_choices":["Grit","Reflex"],  "edge":{"en":"Break the line (1 CHG)",   "fr":"Casse la ligne (1 CHG)"}},
    "Triggerhand":  {"desc":{"en":"Precision shooter",       "fr":"Tir de précision"},           "skills":["Shoot","Observe"], "attr_choices":["Reflex","Face"],  "edge":{"en":"Clean shot (1 CHG)",       "fr":"Tir propre (1 CHG)"}},
    "Ghostwalker":  {"desc":{"en":"Stealthy infiltrator",    "fr":"Infiltrateur furtif"},        "skills":["Stealth","Move"],  "attr_choices":["Reflex","Mind"],  "edge":{"en":"Vanish (1 CHG)",           "fr":"Disparaître (1 CHG)"}},
    "Breaker":      {"desc":{"en":"Hacker / system breaker", "fr":"Hacker / briseur de systèmes"}, "skills":["Hack","Observe"],  "attr_choices":["Mind","Reflex"],  "edge":{"en":"Chain access (1 CHG)",     "fr":"Accès en chaîne (1 CHG)"}},
    "Talkknife":    {"desc":{"en":"Social manipulator",      "fr":"Manipulateur social"},        "skills":["Talk","Street"],   "attr_choices":["Face","Mind"],    "edge":{"en":"Flip the ask (1 CHG)",     "fr":"Retourner la demande (1 CHG)"}},
    "Wiredoc":      {"desc":{"en":"Street doc / tinkerer",   "fr":"Doc de rue / bricoleur"},     "skills":["Med","Craft"],     "attr_choices":["Mind","Grit"],    "edge":{"en":"Patch protocol (1 CHG)",   "fr":"Protocole de patch (1 CHG)"}},
}

BACKGROUNDS = {
    "Streetborn":    {"perk":{"en":"1 contact",        "fr":"1 contact"},          "boosts":["Street","Stealth"]},
    "Corpo Ledger":  {"perk":{"en":"Access token",     "fr":"Jeton d’accès"},      "boosts":["Talk","Observe"]},
    "Ghost-Runner":  {"perk":{"en":"Bolt-hole",        "fr":"Planque (bolt-hole)"},"boosts":["Hack","Stealth"]},
    "Clinic Debt":   {"perk":{"en":"Free patch",       "fr":"Patch gratuit"},      "boosts":["Med","Grit"]},
    "Courier Loop":  {"perk":{"en":"Knows the routes", "fr":"Connaît les trajets"},"boosts":["Move","Observe"]},
    "Salvage Faith": {"perk":{"en":"Tech intuition",   "fr":"Intuition techno"},   "boosts":["Craft","Mind"]},
}

STARTING_GEAR = [
    {"name":"Reinforced Jacket",  "type":"armor",      "arm":1, "cost": 400, "desc":"ARM 1"},
    {"name":"Armor Jacket",       "type":"armor",      "arm":2, "cost": 800, "desc":"ARM 2"},
    {"name":"Tactical Vest",      "type":"armor",      "arm":3, "cost":1200, "desc":"ARM 3 (−2 Stealth)"},
    {"name":"Knife/Shiv",         "type":"weapon",            "cost": 300, "desc":"d6 melee, Concealable"},
    {"name":"Baton/Pipe",         "type":"weapon",            "cost": 500, "desc":"d8 melee, Stun"},
    {"name":"Holdout Pistol",     "type":"weapon",            "cost": 500, "desc":"d6 ranged, Concealable"},
    {"name":"Service Pistol",     "type":"weapon",            "cost": 800, "desc":"d8 ranged, Reliable"},
    {"name":"Med Kit",            "type":"tool",              "cost": 500, "desc":"Unlocks Med actions"},
    {"name":"Bypass Kit",         "type":"tool",              "cost": 500, "desc":"Unlocks electronic hacking"},
    {"name":"Stim Patch",         "type":"consumable",        "cost": 200, "desc":"+2 physical or +2 CHG", "max":2},
    {"name":"Trauma Gel",         "type":"consumable",        "cost": 300, "desc":"Stops Bleeding, heals 1d6 VIT"},
]

PLAYBOOK_CRED_BONUS = {"Talkknife":400, "Triggerhand":300, "Riotline":200, "Breaker":200, "Ghostwalker":100, "Wiredoc":100}
PLAYBOOK_STARTER_GEAR = {
    "Riotline":     [{"name":"Baton/Pipe",     "type":"weapon", "cost":0, "desc":"d8 melee, Stun"}],
    "Triggerhand":  [{"name":"Holdout Pistol", "type":"weapon", "cost":0, "desc":"d6 ranged, Concealable"}],
    "Ghostwalker":  [{"name":"Knife/Shiv",     "type":"weapon", "cost":0, "desc":"d6 melee, Concealable"}],
    "Breaker":      [{"name":"Bypass Kit",     "type":"tool",   "cost":0, "desc":"Unlocks electronic hacking"}],
    "Talkknife":    [{"name":"Holdout Pistol", "type":"weapon", "cost":0, "desc":"d6 ranged, Concealable"}],
    "Wiredoc":      [{"name":"Med Kit",        "type":"tool",   "cost":0, "desc":"Unlocks Med actions"}],
}
BACKGROUND_CRED_MOD = {"Corpo Ledger":300, "Courier Loop":100, "Ghost-Runner":0, "Salvage Faith":0, "Streetborn":-200, "Clinic Debt":-200}

ATTR_PACKAGES = {
    "Specialist":      {"dice":[12,10,6,4], "desc":{"en":"d12/d10/d6/d4 — one dominant trait",  "fr":"d12/d10/d6/d4 — un trait dominant"}},
    "Balanced":        {"dice":[10, 8,8,6], "desc":{"en":"d10/d8/d8/d6 — balanced",             "fr":"d10/d8/d8/d6 — équilibré"}},
    "Tough Generalist":{"dice":[10,10,6,6], "desc":{"en":"d10/d10/d6/d6 — two solid traits",    "fr":"d10/d10/d6/d6 — deux traits solides"}},
}

ATTRS      = ["Grit","Reflex","Mind","Face"]
ALL_SKILLS = ["Fight","Shoot","Move","Stealth","Hack","Observe","Talk","Threaten","Craft","Operate","Street","Med"]
CONDITIONS = ["Bruised", "Bleeding", "Shaken", "Crippled", "Critical"]
ATTR_DISPLAY = {"en": {"Grit":"Grit", "Reflex":"Reflex", "Mind":"Mind", "Face":"Face"}, "fr": {"Grit":"Cran", "Reflex":"Réflexes", "Mind":"Esprit", "Face":"Prestance"}}
SKILL_DISPLAY = {"en": {k:k for k in ALL_SKILLS}, "fr": {"Fight":"Combat", "Shoot":"Tir", "Move":"Mouvement", "Stealth":"Furtivité", "Hack":"Piratage", "Observe":"Observation", "Talk":"Discussion", "Threaten":"Intimidation", "Craft":"Fabrication", "Operate":"Opération", "Street":"Rue", "Med":"Médecine"}}

def _attr_label(lang, key):  return ATTR_DISPLAY.get(lang, ATTR_DISPLAY["en"]).get(key, key)
def _skill_label(lang, key): return SKILL_DISPLAY.get(lang, SKILL_DISPLAY["en"]).get(key, key)
DIE_STEPS = [4, 6, 8, 10, 12]
def _step_up_die(current):
    idx = DIE_STEPS.index(current) if current in DIE_STEPS else 0
    return DIE_STEPS[min(idx + 1, len(DIE_STEPS) - 1)]
def _find_skill_tier(skills, skill):
    for tier in ("d12","d10","d8","d6","d4"):
        if skill in skills.get(tier, []): return tier
    return None

class Character:
    def __init__(self):
        self.name       = ""
        self.playbook   = ""
        self.background = ""
        self.bio        = ""
        self.anchor     = ""
        self.scar       = ""
        self.attrs      = {}
        self.skills     = {"d12":[], "d10":[], "d8":[], "d6":[], "d4":[]}
        self.max_vit    = 15
        self.max_chg    = 8
        self.arm        = 0
        self.credits    = 0
        self.gear       = []
    def to_dict(self): return self.__dict__.copy()
    @classmethod
    def from_dict(cls, data):
        c = cls(); c.__dict__.update(data); return c

class TTSManager:
    def __init__(self, env):
        self.engine     = env.get("TTS_ENGINE", "off")
        self.api_key    = env.get("GOOGLE_TTS_API_KEY", "")
        self.enabled    = self.engine != "off"
        self.last_error = None
        self.lang       = "en"
        self.npc_ages   = {}
        self.url = "https://texttospeech.googleapis.com/v1/text:synthesize?key=" + self.api_key

    def synthesize_to_file(self, text, gender="neutral", npc_name=None, emotion=None, age=None):
        if not self.enabled or not str(text).strip(): return None
        try:
            clean = _normalize_for_tts(text)
            
            # --- Prosody calculations ---
            if npc_name:
                if npc_name not in self.npc_ages:
                    self.npc_ages[npc_name] = AGE_KEYWORDS.get(age, "adult") if age else "adult"
                resolved_age = self.npc_ages[npc_name]
            else:
                resolved_age = AGE_KEYWORDS.get(age, "adult") if age else "adult"

            # Lock the narrator's voice so it doesn't shift emotions
            if gender == "narrator":
                final_rate = 0.97
                final_pitch = -0.6
                emphasis = "none"
            else:
                emo = EMOTION_PROFILES.get(emotion, EMOTION_PROFILES[_infer_emotion(clean)])
                age_rate = AGE_RATE_MOD.get(resolved_age, 1.0)
                final_rate  = emo["rate"] * GENDER_RATE.get(gender, 1.0) * age_rate
                final_pitch = emo["pitch"] + GENDER_PITCH.get(gender, 0.0) + AGE_PITCH_OFFSET.get(resolved_age, 0.0)
                emphasis = emo["emphasis"]

            if self.engine == "edge":
                pool = EDGE_VOICES.get(self.lang, EDGE_VOICES["en"]).get(gender, EDGE_VOICES["en"]["neutral"])
                key = npc_name if npc_name else "narrator" if gender == "narrator" else clean[:40]
                voice_name = _stable_pick(key, pool) or pool[0]
                
                rate_pct = int(round((final_rate - 1.0) * 100))
                pitch_hz = int(round(final_pitch * 4)) 
                
                cmd = [
                    "edge-tts", "--voice", voice_name,
                    "--rate", f"{rate_pct:+d}%",
                    "--pitch", f"{pitch_hz:+d}Hz",
                    "--text", clean
                ]
                fd, tmp = tempfile.mkstemp(suffix=".wav"); os.close(fd)
                cmd.extend(["--write-media", tmp])
                try:
                    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    return tmp
                except Exception:
                    if os.path.exists(tmp): os.unlink(tmp)
                    return None

            elif self.engine == "google":
                v = TTS_VOICES[self.lang]
                pools = TTS_POOLS.get(self.lang, TTS_POOLS["en"])
                pool  = pools.get(gender, pools.get("neutral", [v["neutral"]]))
                key = npc_name if npc_name else "narrator" if gender == "narrator" else clean[:40]
                voice_name = _stable_pick(key, pool) or pool[0]
                
                # Restore the SSML tags for Neural2
                body = clean.replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")
                if emphasis != "none": body = f"<emphasis level='{emphasis}'>{body}</emphasis>"
                rate_str  = str(int(round(max(0.25, min(4.0, final_rate)) * 100))) + "%"
                pitch_str = "{:+.1f}st".format(max(-6.0, min(6.0, final_pitch)))
                body = f"<prosody rate='{rate_str}' pitch='{pitch_str}'>{body}</prosody>"
                
                ssml = f"<speak><lang xml:lang='{v['lang']}'>{body}</lang></speak>" if self.lang == "fr" else f"<speak>{body}</speak>"
                
                payload = {
                    "input": {"ssml": ssml},
                    "voice": {"languageCode": v["lang"], "name": voice_name},
                    "audioConfig": {"audioEncoding": "LINEAR16", "sampleRateHertz": SAMPLE_RATE}
                }
                
                resp = requests.post(self.url, json=payload, timeout=15)
                if resp.status_code == 200:
                    audio_bytes = base64.b64decode(resp.json()["audioContent"])
                    fd, tmp = tempfile.mkstemp(suffix=".wav"); os.close(fd)
                    with wave.open(tmp,"wb") as wf:
                        wf.setnchannels(1); wf.setsampwidth(2); wf.setframerate(SAMPLE_RATE); wf.writeframes(audio_bytes)
                    return tmp
                else:
                    self.last_error = f"TTS {resp.status_code}: {resp.text[:200]}"
                    return None
                    
        except Exception as e:
            self.last_error = str(e)
            return None

    def toggle(self):
        if self.engine != "off":
            self.enabled = not self.enabled

# ============================================================
# LLM ADAPTER
# ============================================================
AVAILABLE_MODELS = [
    {"id": "gemini-2.5-pro",            "label": "Gemini 2.5 Pro",     "key": "GEMINI_API_KEY"},
    {"id": "claude-sonnet-4-5-20250929","label": "Claude Sonnet 4.5",  "key": "ANTHROPIC_API_KEY"},
    {"id": "local-model",               "label": "Local LLM",          "key": "OPENAI_API_BASE"},
]

class LLMAdapter:
    def __init__(self, model_id, env):
        self.model_id = model_id
        if model_id.startswith("gemini"):
            self.provider = "gemini"
            self.client = genai.Client(api_key=env["GEMINI_API_KEY"])
        elif model_id == "local-model":
            self.provider = "openai"
            if not OPENAI_AVAILABLE: raise ImportError("openai package not installed.")
            self.client = _openai.OpenAI(api_key="local-key", base_url=env["OPENAI_API_BASE"])
        else:
            self.provider = "anthropic"
            if not ANTHROPIC_AVAILABLE: raise ImportError("anthropic package not installed.")
            self.client = _anthropic.Anthropic(api_key=env["ANTHROPIC_API_KEY"])

    def generate(self, prompt, max_tokens=4096, system_prompt=None, messages=None):
        if self.provider == "gemini":
            resp = self.client.models.generate_content(
                model=self.model_id,
                contents=prompt,
                config=types.GenerateContentConfig(temperature=0.7, max_output_tokens=max_tokens)
            )
            text = (getattr(resp, "text", None) or "").strip()
            return text, getattr(getattr(resp, "usage_metadata", None), "prompt_token_count", 0), getattr(getattr(resp, "usage_metadata", None), "candidates_token_count", 0)
        elif self.provider == "openai":
            resp = self.client.chat.completions.create(
                model=self.model_id,
                messages=messages if messages else [{"role": "user", "content": prompt}],
                temperature=0.7, max_tokens=max_tokens
            )
            return resp.choices[0].message.content.strip(), resp.usage.prompt_tokens, resp.usage.completion_tokens
        else:
            kwargs = {"model": self.model_id, "max_tokens": max_tokens, "temperature": 0.7, "messages": messages or [{"role": "user", "content": prompt}]}
            if system_prompt and messages: kwargs["system"] = system_prompt
            resp = self.client.messages.create(**kwargs)
            return resp.content[0].text.strip(), resp.usage.input_tokens, resp.usage.output_tokens

# ============================================================
# GM -- AI Game Master 
# ============================================================
class Nexus9GM:
    def __init__(self, model_id, character, rules_text, world_text, lang="en"):
        self.adapter = LLMAdapter(model_id, ENV)
        self.model_id = model_id
        self.character = character
        self.rules = rules_text
        self.world = world_text
        self.lang = lang
        self.history = []
        self.heat = 0
        self.conditions = []
        self.credits = getattr(character, 'credits', 0)
        self.pending_tn = None
        self.scene_counter = 0
        self.last_scene_type = "NONE"
        self.scene_history = []
        self.session_cost = 0.0

    def _suggest_next(self):
        if self.last_scene_type == "ACTION": return "DOWNTIME or SOCIAL"
        recent = self.scene_history[-3:]
        if recent.count("ACTION") == 0 and len(recent) >= 2: return "SOCIAL, INVESTIGATION, or TENSION"
        return ["SOCIAL","INVESTIGATION","TENSION","ACTION","DOWNTIME"][self.scene_counter % 5]

    def _system_prompt(self):
        c = self.character
        lang_block = ""
        if self.lang == "fr":
            lang_block = (
                "\n=== LANGUE ===\n"
                "Respond entirely in French. Use these terms:\n"
                "Attrs: Grit→Cran, Reflex→Réflexes, Mind→Esprit, Face→Prestance.\n"
                "Skills: Fight→Combat, Shoot→Tir, Move→Mouvement, Stealth→Furtivité, "
                "Hack→Piratage, Observe→Observation, Talk→Discussion, Threaten→Intimidation, "
                "Craft→Fabrication, Operate→Opération, Street→Rue, Med→Médecine.\n"
                "Example: 'Lance Cran + Combat, TN 11'.\n"
                "Keep proper nouns in English.\n"
            )
        return (
            "You are the Game Master for NEXUS-9, a dark cyberpunk RPG.\n\n"
            "=== RULES ===\n" + self.rules + "\n\n"
            "=== WORLD ===\n" + self.world + "\n\n"
            "=== PC ===\n"
            "Name: " + c.name + " | Playbook: " + c.playbook + " | Background: " + c.background + "\n"
            "Bio: " + getattr(c, "bio", "") + "\n"
            "Thread: " + c.anchor + " | Consequence: " + c.scar + "\n"
            "Attrs: " + str(c.attrs) + " | Skills: " + str(c.skills) + "\n"
            "Gear: " + (", ".join(c.gear) if c.gear else "None") + "\n"
            "ARM: " + str(c.arm) + "\n\n"
            "=== STATE ===\n"
            "Heat:" + str(self.heat) + " Creds:" + str(self.credits)
            + " Scene:" + str(self.scene_counter)
            + " Last:" + self.last_scene_type
            + " Next:" + self._suggest_next() + "\n"
            "Conditions: " + (", ".join(self.conditions) if self.conditions else "None") + "\n\n"
            "=== GM DIRECTIVES ===\n"
            "FORMAT: 3-5 paragraphs max. Plain text only — no markdown (*, **, _, #). "
            "NPC dialogue on own line: (M) Name: \"text\" or (F, emotion) Name: \"text\" or (F, emotion, age) Name: \"text\". "
            "Gender M/F required. Emotion always included. Age hint (child/young/old) only first appearance. Never mix dialogue into narration.\n\n"
            "MECHANICS:\n"
            "- Stat tags inline: [VIT +X] [VIT -X] [CHG +X] [CHG -X] [Heat +X] [Heat -X] [Creds +X] [Creds -X]\n"
            "- Conditions: [+Bruised] [+Bleeding] [+Shaken] [+Crippled] [+Critical] [-Bleeding] etc.\n"
            "- UNCERTAIN actions: state Attr+Skill+TN, STOP, wait for roll.\n"
            + lang_block
        )

    def _parse_stat_changes(self, text):
        changes = {}
        for m in re.finditer(r'\[(Heat|VIT|CHG|ARM|Marks|Creds)\s*([+-])\s*(\d+)\]', text, re.IGNORECASE):
            stat = m.group(1).upper()
            changes[stat] = changes.get(stat, 0) + (1 if m.group(2) == '+' else -1) * int(m.group(3))
        if "HEAT" in changes: self.heat = max(0, self.heat + changes["HEAT"])
        if "CREDS" in changes: self.credits = max(0, self.credits + changes["CREDS"])
        for m in re.finditer(r'\[([+-])(' + '|'.join(re.escape(c) for c in CONDITIONS) + r')\]', text):
            if m.group(1) == '+' and m.group(2) not in self.conditions: self.conditions.append(m.group(2))
            elif m.group(1) == '-' and m.group(2) in self.conditions: self.conditions.remove(m.group(2))
        return changes

    def send(self, user_msg):
        self.history.append({"role":"user", "content": user_msg})
        if len(self.history) > 20: self.history = self.history[:2] + self.history[-18:]
        sys = self._system_prompt()
        prompt = sys + "\n\n" + "\n".join(f"{m['role'].upper()}: {m['content']}" for m in self.history) + "\nASSISTANT:"
        text, inp, out = self.adapter.generate(prompt, system_prompt=sys, messages=self.history)
        self.history.append({"role":"assistant", "content": text})
        self.scene_counter += 1
        self.last_scene_type = "ACTION" if "attack" in text.lower() else "SOCIAL"
        self.scene_history.append(self.last_scene_type)
        return text, 0.0, inp+out, self._parse_stat_changes(text), 0.0

    def launch_campaign(self): return self.send("Launch the campaign.")
    def to_dict(self): return {"model_id": self.model_id, "history": self.history, "heat": self.heat, "conditions": self.conditions, "credits": self.credits, "scene_counter": self.scene_counter, "scene_history": self.scene_history}
    def from_dict(self, data):
        self.history = data.get("history", [])
        self.heat = data.get("heat", 0)
        self.conditions = data.get("conditions", [])
        self.credits = data.get("credits", 0)
        self.scene_counter = data.get("scene_counter", 0)

# ============================================================
# WIDGETS
# ============================================================
class StatsBar(Static):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.name_str  = ""; self.playbook = ""
        self.heat      = 0;  self.scene    = 0
        self.vit = 15; self.max_vit = 15
        self.chg = 8;  self.max_chg = 8
        self.arm = 0;  self.marks = 0
        self.credits   = 0
        self.conditions = []
        self.attrs     = {}   
        self.tts_on = True

    def update_stats(self, **kw):
        for k, v in kw.items():
            if hasattr(self, k): setattr(self, k, v)
        self._refresh()

    def _refresh(self):
        t = Text()
        t.append(self.name_str, style="bold cyan")
        if self.playbook:
            t.append(" -- " + self.playbook, style="cyan")
        t.append("  ")
        hc = "bold red" if self.heat > 5 else "bold yellow" if self.heat > 2 else "bold green"
        t.append("HEAT:" + str(self.heat), style=hc)
        flags = " [TTS]" if self.tts_on else " [---]"
        t.append(flags + "\n")
        vc = "bold red" if self.vit <= 5 else "bold yellow" if self.vit <= 10 else "bold green"
        t.append("VIT:" + str(self.vit) + "/" + str(self.max_vit) + "  ", style=vc)
        cc = "bold red" if self.chg <= 3 else "bold yellow" if self.chg <= 6 else "bold green"
        t.append("CHG:" + str(self.chg) + "/" + str(self.max_chg) + "  ", style=cc)
        t.append("ARM:" + str(self.arm) + "  ", style="bold cyan")
        t.append("MRK:" + str(self.marks), style="bold magenta")
        t.append("  CRD:" + str(self.credits), style="bold yellow")
        t.append("  Scene:" + str(self.scene), style="dim")
        t.append("\n")
        if self.attrs:
            parts = []
            for attr in ["Grit","Reflex","Mind","Face"]:
                if attr in self.attrs:
                    parts.append(attr + ":d" + str(self.attrs[attr]))
            t.append("  ".join(parts), style="dim cyan")
        if self.conditions:
            t.append("\n")
            t.append("CONDITIONS: " + ", ".join(self.conditions), style="bold red")
        self.update(t)

class GameLog(RichLog):
    def __init__(self, **kw):
        super().__init__(highlight=True, markup=True, wrap=True, **kw)
        self.can_focus = False


# ============================================================
# SCREENS (Setup, Language, Models, Save/Load, Char Creation)
# ============================================================
class SetupScreen(Screen):
    CSS = """
    #box { width: 100%; height: auto; background: $surface; border: solid $primary; padding: 1; margin: 1; }
    Static { color: #00ff41; margin: 0 1; }
    Input  { width: 100%; margin: 1; background: #0a0a0a; color: #00ff41; border: solid #00ff41; }
    """
    def compose(self) -> ComposeResult:
        with Container(id="box"):
            yield Static("", id="info")
            yield Input(id="input")

    def on_mount(self):
        self.step = 1
        self.updates = {}
        self._show()

    def _show(self):
        info = self.query_one("#info", Static)
        inp = self.query_one("#input", Input)
        if self.step == 1:
            info.update("=== INITIAL SETUP ===\nNo active configuration found.\n\nChoose AI Provider:\n  1. Gemini (Google)\n  2. Claude (Anthropic)\n  3. Local LLM (Ollama, LM Studio)")
            inp.placeholder = "1-3"
        elif self.step == 2:
            p = self.updates.get("_provider")
            if p == "1":
                info.update("=== GEMINI ===\nEnter your Gemini API Key:")
                inp.placeholder = "AIzaSy..."
            elif p == "2":
                info.update("=== CLAUDE ===\nEnter your Anthropic API Key:")
                inp.placeholder = "sk-ant-..."
            else:
                info.update("=== LOCAL LLM ===\nEnter your local API Base URL\n(e.g., http://localhost:11434/v1):")
                inp.placeholder = "http://localhost:..."
        elif self.step == 3:
            info.update("=== TEXT-TO-SPEECH ===\nChoose TTS Engine:\n  1. Edge TTS (Free, high quality, internet required)\n  2. Google Cloud (Requires Google API Key)\n  3. Disabled (Text only)")
            inp.placeholder = "1-3"
        elif self.step == 4:
            info.update("=== GOOGLE TTS ===\nEnter your Google Cloud API Key:")
            inp.placeholder = "AIzaSy..."

    def on_input_submitted(self, event: Input.Submitted) -> None:
        val = event.value.strip()
        event.input.value = ""
        if self.step == 1:
            if val in ["1", "2", "3"]: self.updates["_provider"] = val; self.step = 2
        elif self.step == 2:
            if not val: return
            p = self.updates.get("_provider")
            if p == "1": self.updates["GEMINI_API_KEY"] = val
            elif p == "2": self.updates["ANTHROPIC_API_KEY"] = val
            elif p == "3": self.updates["OPENAI_API_BASE"] = val
            self.step = 3
        elif self.step == 3:
            if val == "1":
                self.updates["TTS_ENGINE"] = "edge"
                self._finish()
            elif val == "2":
                self.updates["TTS_ENGINE"] = "google"
                self.step = 4
            elif val == "3":
                self.updates["TTS_ENGINE"] = "off"
                self._finish()
        elif self.step == 4:
            if not val: return
            self.updates["GOOGLE_TTS_API_KEY"] = val
            self._finish()
        self._show()

    def _finish(self):
        self.updates.pop("_provider", None)
        save_env(self.updates)
        self.dismiss()

class LanguageScreen(Screen):
    CSS = """
    #box {
        width: 100%; height: auto;
        background: $surface; border: solid $primary;
        padding: 1; margin: 1;
    }
    Static { color: #00ff41; margin: 0 1; }
    Input  { width: 100%; margin: 1;
             background: #0a0a0a; color: #00ff41; border: solid #00ff41; }
    """
    def compose(self) -> ComposeResult:
        with Container(id="box"):
            yield Static("", id="info")
            yield Input(id="input")

    def on_mount(self):
        self.query_one("#info", Static).update(
            "=== NEXUS-9 ===\n\n  1. English\n  2. Francais\n")
        self.query_one("#input", Input).placeholder = "1 or 2"

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        event.input.value = ""
        val = event.value.strip()
        if   val == "1": self.dismiss("en")
        elif val == "2": self.dismiss("fr")

class ModelSelectionScreen(Screen):
    CSS = """
    #box {
        width: 100%; height: auto;
        background: $surface; border: solid $primary;
        padding: 1; margin: 1;
    }
    Static { color: #00ff41; margin: 0 1; }
    Input  { width: 100%; margin: 1;
             background: #0a0a0a; color: #00ff41; border: solid #00ff41; }
    """
    def __init__(self, env):
        super().__init__()
        self.models = [m for m in AVAILABLE_MODELS if env.get(m["key"])]

    def compose(self) -> ComposeResult:
        with Container(id="box"):
            yield Static("", id="info")
            yield Input(id="input")

    def on_mount(self):
        t = "=== CHOOSE AI MODEL ===\n\n"
        for i, m in enumerate(self.models):
            t += "  " + str(i+1) + ". " + m["label"] + "\n"
        if not self.models:
            t += "  No API keys found in .env\n"
        t += "\n"
        self.query_one("#info", Static).update(t)
        self.query_one("#input", Input).placeholder = "1-" + str(len(self.models)) if self.models else "-"

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        event.input.value = ""
        try:
            idx = int(event.value.strip()) - 1
            if 0 <= idx < len(self.models):
                self.dismiss(self.models[idx]["id"])
        except (ValueError, IndexError):
            pass

class SaveChargerScreen(Screen):
    CSS = """
    #box {
        width: 100%; height: auto;
        background: $surface; border: solid $primary;
        padding: 1; margin: 1;
    }
    Static { color: #00ff41; margin: 0 1; }
    Input  { width: 100%; margin: 1;
             background: #0a0a0a; color: #00ff41; border: solid #00ff41; }
    """
    def __init__(self, saves, s):
        super().__init__()
        self.saves = saves
        self.s     = s

    def compose(self) -> ComposeResult:
        with Container(id="box"):
            yield Static("", id="info")
            yield Input(id="input")

    def on_mount(self):
        self._show()

    def _show(self):
        lines = ["\n" + self.s["sl_title"] + "\n"]
        for i, sp in enumerate(self.saves):
            try:
                d    = json.loads(sp.read_text())
                name = d.get("character",{}).get("name","?")
                heat = d.get("gm",{}).get("heat","?")
                sc   = d.get("gm",{}).get("scene_counter","?")
                ts   = d.get("saved_at","?")[:16]
                lines.append("  " + str(i+1) + ". " + name + "  Heat:" + str(heat) + " Scene:" + str(sc) + "  " + ts)
            except Exception:
                lines.append("  " + str(i+1) + ". " + sp.name)
        lines.append("\n" + self.s["sl_prompt"])
        self.query_one("#info", Static).update("\n".join(lines))
        self.query_one("#input", Input).placeholder = "1 / n"

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        val = event.value.strip().lower()
        event.input.value = ""
        if val == "n":
            self.dismiss(None); return
        try:
            idx = int(val) - 1
            if 0 <= idx < len(self.saves):
                self.dismiss(json.loads(self.saves[idx].read_text())); return
        except (ValueError, IndexError, json.JSONDecodeError):
            pass
        self._show()


class CharacterCreationScreen(Screen):
    CSS = """
    #box {
        width: 100%; height: auto;
        background: $surface; border: solid $primary;
        padding: 1; margin: 1;
    }
    Static { color: #00ff41; margin: 0 1; }
    Input  { width: 100%; margin: 1;
             background: #0a0a0a; color: #00ff41; border: solid #00ff41; }
    """
    def __init__(self, s, lang="en"):
        super().__init__()
        self.s           = s
        self.lang        = lang
        self.char        = Character()
        self.step        = 1
        self.attr_pool   = []
        self.attr_assign = {}
        self.sk          = {"d12":[], "d10":[], "d8":[], "d6":[], "d4":[]}
        self.pb_attr_choice = None
        self.pb_log         = []
        self.bg_log         = []
        self.starting_credits = 0
        self.d20_roll       = 0
        self.gear_picked    = []
        self.gear_spent     = 0
        self.gear_error     = ""

    def compose(self) -> ComposeResult:
        with Container(id="box"):
            yield Static("", id="info")
            yield Input(id="input")

    def on_mount(self):
        self._show()

    def _show(self):
        info = self.query_one("#info", Static)
        inp  = self.query_one("#input", Input)

        if self.step == 99:
            t = self.s["cc_bio_title"] + "\n\n" + self.s["cc_bio_sub"] + "\n\n"
            info.update(t)
            inp.placeholder = "Bio..."
            return

        inp.value = ""
        c = self.char
        s = self.s

        if self.step == 1:
            info.update("=== NEXUS-9 ===\n\n" + s["cc_name"])
            inp.placeholder = "Name..."

        elif self.step == 2:
            t = s["cc_playbook"] + "\n\n"
            for i,(name,d) in enumerate(PLAYBOOKS.items()):
                t += "  " + str(i+1) + ". " + name + " -- " + d["desc"][self.lang] + "\n"
                t += "      " + s["label_skills"] + " " + ", ".join(d["skills"]) + " " + s["label_plus1each"] + "\n"
                t += "      " + s["label_attr"] + " " + s["or_word"].join(d["attr_choices"]) + " +1\n"
                t += "      " + s["label_edge"] + " " + d["edge"][self.lang] + "\n"
            t += "\n" + s["cc_enter_number"]
            info.update(t); inp.placeholder = "1-6"

        elif self.step == 3:
            t = s["cc_bg"] + "\n\n"
            for i,(name,d) in enumerate(BACKGROUNDS.items()):
                t += "  " + str(i+1) + ". " + name + " -- " + d["perk"][self.lang] + "\n"
                t += "      " + s["label_boosts"] + " " + ", ".join(d["boosts"]) + "\n"
            t += "\n" + s["cc_enter_number"]
            info.update(t); inp.placeholder = "1-6"

        elif self.step == 4:
            t = s["cc_attrpkg"] + "\n\n"
            for i,(name,d) in enumerate(ATTR_PACKAGES.items()):
                t += "  " + str(i+1) + ". " + name + ": " + d["desc"][self.lang] + "\n"
            t += "\n" + s["cc_enter_number"]
            info.update(t); inp.placeholder = "1-3"

        elif self.step == 5:
            assigned  = dict(self.attr_assign)
            remaining = list(self.attr_pool)
            for v in assigned.values():
                if v in remaining: remaining.remove(v)
            remaining.sort(reverse=True)
            t  = s["cc_assign"] + "\n\n"
            t += s["label_pool"] + " " + str(self.attr_pool) + "\n\n"
            for attr in ATTRS:
                lab = _attr_label(getattr(self, "lang", "en"), attr)
                if attr in assigned: t += "  + " + lab + ": d" + str(assigned[attr]) + "\n"
                else:                t += "  ? " + lab + ": ___\n"
            if remaining:
                unassigned = [a for a in ATTRS if a not in assigned]
                t += "\n" + s["cc_assign_q"].format(die=remaining[0]) + "\n"
                for i,a in enumerate(unassigned):
                    t += "  " + str(i+1) + ". " + _attr_label(getattr(self, "lang", "en"), a) + "\n"
                inp.placeholder = "1-" + str(len(unassigned))
            info.update(t)

        elif self.step == 6:
            t  = s["cc_sk10_title"] + "\n\n" + s["cc_sk10_sub"] + "\n\n"
            for i,sk in enumerate(ALL_SKILLS):
                t += "  " + str(i+1) + ". " + _skill_label(getattr(self, "lang", "en"), sk) + "\n"
            t += "\n" + s["cc_enter_two"]
            info.update(t); inp.placeholder = "1,3"

        elif self.step == 7:
            rem = [sk for sk in ALL_SKILLS if sk not in self.sk["d10"]]
            t  = s["cc_sk8_title"] + "\n\n" + s["cc_sk8_sub"] + "\n\n"
            for i,sk in enumerate(rem):
                t += "  " + str(i+1) + ". " + _skill_label(getattr(self, "lang", "en"), sk) + "\n"
            t += "\n" + s["cc_enter_three"]
            info.update(t); inp.placeholder = "1,2,4"

        elif self.step == 8:
            rem = [sk for sk in ALL_SKILLS if sk not in self.sk["d10"] and sk not in self.sk["d8"]]
            t  = s["cc_sk6_title"] + "\n\n" + s["cc_sk6_sub"] + "\n\n"
            for i,sk in enumerate(rem):
                t += "  " + str(i+1) + ". " + _skill_label(getattr(self, "lang", "en"), sk) + "\n"
            t += "\n" + s["cc_enter_three"]
            info.update(t); inp.placeholder = "1,2,4"

        elif self.step == 9:
            pb = PLAYBOOKS[c.playbook]
            t  = s["cc_pb_boost_title"] + "\n\n"
            t += "  " + c.playbook + " " + s["cc_pb_grants"] + "\n"
            t += "  " + s["cc_pb_boost_skills"] + " " + ", ".join(pb["skills"]) + "\n\n"
            t += "  " + s["cc_pb_boost_attr"] + "\n"
            for i, attr in enumerate(pb["attr_choices"]):
                t += "    " + str(i+1) + ". " + attr + " +1\n"
            info.update(t); inp.placeholder = "1-2"

        elif self.step == 10:
            t  = s["cc_boosts_title"] + "\n\n"
            t += "  Your Playbook and Background each upgrade some of your dice.\n"
            t += "  Each step moves one size up: d4 → d6 → d8 → d10 → d12.\n\n"
            t += "  " + c.playbook + " (Playbook):\n"
            if self.pb_log:
                for entry in self.pb_log:
                    t += "    " + entry + "\n"
            else:
                t += "    (already at max)\n"
            t += "\n  " + c.background + " (Background):\n"
            if self.bg_log:
                for entry in self.bg_log:
                    t += "    " + entry + "\n"
            else:
                t += "    (already at max)\n"
            t += "\n  VIT:" + str(c.max_vit) + "  CHG:" + str(c.max_chg) + "\n"
            t += "\n" + s["cc_boosts_continue"]
            info.update(t); inp.placeholder = "Enter..."

        elif self.step == 14:
            pb_bonus = PLAYBOOK_CRED_BONUS.get(c.playbook, 0)
            bg_mod   = BACKGROUND_CRED_MOD.get(c.background, 0)
            t  = s["cc_credits_title"] + "\n\n"
            t += "  d20 roll : " + str(self.d20_roll) + " × 100 = " + str(self.d20_roll * 100) + "\n"
            t += "  " + c.playbook + " bonus : +" + str(pb_bonus) + "\n"
            sign = "+" if bg_mod >= 0 else ""
            t += "  " + c.background + " : " + sign + str(bg_mod) + "\n"
            t += "  ─────────────────────\n"
            t += "  Budget : " + str(self.starting_credits) + " Creds (min 500)\n"
            if self.gear_picked:
                t += "\n  Starter kit (" + c.playbook + "): "
                t += ", ".join(g["name"] for g in self.gear_picked) + " [free]\n"
            t += "\n" + s["cc_boosts_continue"]
            info.update(t); inp.placeholder = "Enter..."

        elif self.step == 15:
            remaining = self.starting_credits - self.gear_spent
            cur_armor = None
            for g in self.gear_picked:
                if g["type"] == "armor":
                    cur_armor = g; break

            t  = s["cc_gear_title"] + "\n\n"
            t += "  Budget: " + str(self.starting_credits) + "  Spent: " + str(self.gear_spent) + "  Left: " + str(remaining) + "\n\n"

            for i, item in enumerate(STARTING_GEAR):
                flag = ""
                if item["type"] == "armor":
                    if cur_armor and cur_armor["name"] == item["name"]:
                        flag = " [equipped]"
                    else:
                        eff_cost = item["cost"] - (cur_armor["cost"] if cur_armor else 0)
                        if eff_cost > remaining: flag = " [X]"
                elif "max" in item:
                    count = sum(1 for g in self.gear_picked if g["name"] == item["name"])
                    if count >= item["max"]:  flag = " [maxed]"
                    elif item["cost"] > remaining: flag = " [X]"
                else:
                    if any(g["name"] == item["name"] for g in self.gear_picked):
                        flag = " [owned]"
                    elif item["cost"] > remaining: flag = " [X]"

                max_tag = " (×" + str(item["max"]) + " max)" if "max" in item else ""
                num = str(i+1) + "."
                t += "  " + num.ljust(4) + item["name"] + max_tag + "  —  " + item["desc"] + "  —  " + str(item["cost"]) + " Creds" + flag + "\n"

            t += "\n  " + s["cc_gear_picked"] + " "
            if self.gear_picked:
                for g in self.gear_picked:
                    t += g["name"] + " (" + str(g["cost"]) + ")  "
            else:
                t += s["cc_gear_none"]
            t += "\n"
            if self.gear_error:
                t += "\n  [red]" + self.gear_error + "[/red]\n"
            t += "\n  " + s["cc_gear_prompt"]
            info.update(t); inp.placeholder = "# or done"

        elif self.step == 11:
            info.update(s["cc_anchor"] + "\n\n" + s["cc_anchor_sub"])
            inp.placeholder = "..."

        elif self.step == 12:
            info.update(s["cc_scar"] + "\n\n" + s["cc_scar_sub"])
            inp.placeholder = "..."

        elif self.step == 13:
            t  = s["cc_summary"] + "\n\n"
            t += "  " + c.name + " -- " + c.playbook + " (" + c.background + ")\n\n"
            t += "  " + ", ".join(_attr_label("en", k) + ":d" + str(v) for k,v in c.attrs.items()) + "\n"
            for tier in ["d12","d10","d8","d6","d4"]:
                if c.skills.get(tier):
                    t += "  " + tier + ": " + ", ".join(_skill_label("en", sk) for sk in c.skills[tier]) + "\n"
            t += "\n  VIT:" + str(c.max_vit) + "  CHG:" + str(c.max_chg) + "  ARM:" + str(c.arm) + "  CRD:" + str(c.credits) + "\n"
            if c.gear:
                t += "  Gear: " + ", ".join(c.gear) + "\n"
            t += "  " + s["label_edge"] + " " + PLAYBOOKS[c.playbook]["edge"][self.lang] + "\n\n"
            t += "  " + s["label_anchor"] + " " + c.anchor + "\n"
            t += "  " + s["label_scar"] + " " + c.scar + "\n\n"
            t += s["cc_begin"]
            info.update(t); inp.placeholder = "Enter..."

    def _apply_boosts(self):
        c   = self.char
        pb  = PLAYBOOKS[c.playbook]
        bg  = BACKGROUNDS[c.background]
        pb_log = []
        bg_log = []

        def _bump_skill(sk, target_log):
            old = _find_skill_tier(c.skills, sk)
            if not old: return
            new_die  = _step_up_die(int(old[1:]))
            new_tier = "d" + str(new_die)
            if new_tier == old: return
            c.skills[old].remove(sk)
            c.skills.setdefault(new_tier, []).append(sk)
            target_log.append(_skill_label("en", sk) + " : " + old + " → " + new_tier)

        def _bump_attr(attr, target_log):
            old = c.attrs[attr]
            new = _step_up_die(old)
            if new == old: return
            c.attrs[attr] = new
            target_log.append(_attr_label("en", attr) + " : d" + str(old) + " → d" + str(new))

        for sk in pb["skills"]:
            _bump_skill(sk, pb_log)
        _bump_attr(self.pb_attr_choice, pb_log)

        for boost in bg["boosts"]:
            if boost in ATTRS:
                _bump_attr(boost, bg_log)
            else:
                _bump_skill(boost, bg_log)

        c.max_vit = 10 + c.attrs.get("Grit", 4)
        c.max_chg =  6 + c.attrs.get("Mind", 4)

        self.pb_log = pb_log
        self.bg_log = bg_log

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        val = event.value.strip()
        event.input.value = ""
        c = self.char

        if not val and self.step not in (10, 14, 13):
            return

        if self.step == 1:
            if not val.strip(): return
            c.name = val; self.step = 2

        elif self.step == 2:
            try:
                idx = int(val) - 1
                keys = list(PLAYBOOKS.keys())
                if idx < 0 or idx >= len(keys): return
                c.playbook = keys[idx]; self.step = 3
            except (ValueError, IndexError): return

        elif self.step == 3:
            try:
                idx = int(val) - 1
                keys = list(BACKGROUNDS.keys())
                if idx < 0 or idx >= len(keys): return
                c.background = keys[idx]; self.step = 99
            except (ValueError, IndexError): return

        elif self.step == 99:
            c.bio = val[:280]
            self.step = 4

        elif self.step == 4:
            try:
                idx = int(val) - 1
                vals = list(ATTR_PACKAGES.values())
                if idx < 0 or idx >= len(vals): return
                pkg = vals[idx]
                self.attr_pool   = sorted(pkg["dice"], reverse=True)
                self.attr_assign = {}; self.step = 5
            except (ValueError, IndexError): return

        elif self.step == 5:
            try:
                unassigned = [a for a in ATTRS if a not in self.attr_assign]
                idx = int(val) - 1
                if idx < 0 or idx >= len(unassigned): return
                attr = unassigned[idx]
                remaining = list(self.attr_pool)
                for v in self.attr_assign.values():
                    if v in remaining: remaining.remove(v)
                remaining.sort(reverse=True)
                self.attr_assign[attr] = remaining[0]
                if len(self.attr_assign) == len(ATTRS):
                    c.attrs = dict(self.attr_assign)
                    self.step = 6
            except (ValueError, IndexError): return

        elif self.step == 6:
            try:
                indices = [int(x.strip())-1 for x in val.split(",")]
                if len(indices) != 2: return
                if len(set(indices)) != len(indices): return
                if any(i < 0 or i >= len(ALL_SKILLS) for i in indices): return
                chosen = [ALL_SKILLS[i] for i in indices]
                self.sk["d10"] = chosen; c.skills["d10"] = chosen; self.step = 7
            except (ValueError, IndexError): return

        elif self.step == 7:
            try:
                rem = [sk for sk in ALL_SKILLS if sk not in self.sk["d10"]]
                indices = [int(x.strip())-1 for x in val.split(",")]
                if len(indices) != 3: return
                if len(set(indices)) != len(indices): return
                if any(i < 0 or i >= len(rem) for i in indices): return
                chosen = [rem[i] for i in indices]
                self.sk["d8"] = chosen; c.skills["d8"] = chosen
                self.step = 8
            except (ValueError, IndexError): return

        elif self.step == 8:
            try:
                rem = [sk for sk in ALL_SKILLS if sk not in self.sk["d10"] and sk not in self.sk["d8"]]
                indices = [int(x.strip())-1 for x in val.split(",")]
                if len(indices) != 3: return
                if len(set(indices)) != len(indices): return
                if any(i < 0 or i >= len(rem) for i in indices): return
                chosen = [rem[i] for i in indices]
                self.sk["d6"] = chosen; c.skills["d6"] = chosen
                c.skills["d4"] = [sk for sk in ALL_SKILLS if sk not in self.sk["d10"] and sk not in self.sk["d8"] and sk not in chosen]
                self.step = 9
            except (ValueError, IndexError): return

        elif self.step == 9:
            try:
                pb = PLAYBOOKS[c.playbook]
                idx = int(val) - 1
                if idx < 0 or idx >= len(pb["attr_choices"]): return
                self.pb_attr_choice = pb["attr_choices"][idx]
                self._apply_boosts()
                self.step = 10
            except (ValueError, IndexError): return

        elif self.step == 10:
            pb_bonus = PLAYBOOK_CRED_BONUS.get(c.playbook, 0)
            bg_mod   = BACKGROUND_CRED_MOD.get(c.background, 0)
            self.d20_roll = random.randint(1, 20)
            self.starting_credits = max(500, self.d20_roll * 100 + pb_bonus + bg_mod)
            self.gear_picked = list(PLAYBOOK_STARTER_GEAR.get(c.playbook, []))
            self.gear_spent  = 0
            self.gear_error  = ""
            self.step = 14

        elif self.step == 14:
            self.step = 15

        elif self.step == 15:
            v = val.strip().lower()
            if v == "done":
                c.arm = 0
                for g in self.gear_picked:
                    if g["type"] == "armor":
                        c.arm = g["arm"]; break
                c.gear    = [g["name"] for g in self.gear_picked]
                c.credits = self.starting_credits - self.gear_spent
                self.step = 11
            else:
                self.gear_error = ""
                try:
                    idx  = int(v) - 1
                    if idx < 0 or idx >= len(STARTING_GEAR):
                        self._show(); return
                    item = STARTING_GEAR[idx]
                    remaining = self.starting_credits - self.gear_spent

                    if item["type"] == "armor":
                        cur_armor = None
                        for g in self.gear_picked:
                            if g["type"] == "armor":
                                cur_armor = g; break
                        if cur_armor and cur_armor["name"] == item["name"]:
                            self.gear_error = self.s["cc_gear_dup_armor"]
                        else:
                            eff_cost = item["cost"] - (cur_armor["cost"] if cur_armor else 0)
                            if eff_cost > remaining:
                                self.gear_error = self.s["cc_gear_cant_afford"]
                            else:
                                if cur_armor:
                                    self.gear_spent  -= cur_armor["cost"]
                                    self.gear_picked.remove(cur_armor)
                                self.gear_picked.append(item)
                                self.gear_spent += item["cost"]
                    elif "max" in item:
                        count = sum(1 for g in self.gear_picked if g["name"] == item["name"])
                        if count >= item["max"]:
                            self.gear_error = self.s["cc_gear_dup_armor"]
                        elif item["cost"] > remaining:
                            self.gear_error = self.s["cc_gear_cant_afford"]
                        else:
                            self.gear_picked.append(item)
                            self.gear_spent += item["cost"]
                    else:
                        if any(g["name"] == item["name"] for g in self.gear_picked):
                            self.gear_error = self.s["cc_gear_dup_armor"]
                        elif item["cost"] > remaining:
                            self.gear_error = self.s["cc_gear_cant_afford"]
                        else:
                            self.gear_picked.append(item)
                            self.gear_spent += item["cost"]
                except (ValueError, IndexError):
                    pass

        elif self.step == 11:
            c.anchor = val; self.step = 12

        elif self.step == 12:
            c.scar = val; self.step = 13

        elif self.step == 13:
            self.dismiss(c); return

        self._show()

# ============================================================
# MAIN APP
# ============================================================
class Nexus9App(App):
    CSS = """
    Screen {
        background: #0a0a0a;
    }
    StatsBar {
        height: 5;
        background: #000000;
        color: #00ff41;
        border: solid #00ff41;
        padding: 0 1;
    }
    GameLog {
        height: 1fr;
        background: #000000;
        color: #00ff41;
        border: solid #00ff41;
        padding: 1 1;
        scrollbar-size: 1 1;
        scrollbar-gutter: stable;
    }
    Input {
        background: #0a0a0a;
        color: #00ff41;
        border: solid #00ff41;
    }
    Input:focus {
        border: solid #dc143c;
    }
    """
    BINDINGS = [
        Binding("ctrl+c",  "quit",         "Quit"),
        Binding("ctrl+s",  "save",         "Save"),
        Binding("ctrl+m",  "toggle_tts",   "Mute"),
    ]

    def __init__(self):
        super().__init__()
        self.lang       = "en"
        self.selected_model = None
        self.gm         = None
        self.tts        = None
        self.character  = None
        self.processing = False
        self.transcript = []
        self._rules     = ""
        self._world     = ""

    def compose(self) -> ComposeResult:
        yield StatsBar(id="stats")
        yield GameLog(id="log")
        yield Input(placeholder="> ...", id="input")

    def on_mount(self):
        has_ai = any(ENV.get(k) for k in ["GEMINI_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_BASE"])
        if not has_ai:
            self.push_screen(SetupScreen(), self._on_setup_done)
        else:
            self.push_screen(LanguageScreen(), self._on_lang_choice)

    def _on_setup_done(self, _=None):
        global ENV
        ENV = load_env()
        self.push_screen(LanguageScreen(), self._on_lang_choice)

    def _on_lang_choice(self, lang):
        if lang is None: lang = "en"
        self.lang = lang
        s   = STRINGS[self.lang]
        log = self.query_one("#log", GameLog)

        try:
            self._rules = RULES_PATH.read_text(encoding="utf-8")
            self._world = WORLD_PATH.read_text(encoding="utf-8")
            log.write("[green]+ Rules & world[/green]")
        except FileNotFoundError as e:
            log.write("[red]! " + str(e) + "[/red]"); return

        self.tts = TTSManager(ENV)
        self.tts.lang = self.lang
        if self.tts.enabled:
            log.write("[green]+ TTS (M to mute)[/green]")
        else:
            log.write("[yellow]? TTS not configured[/yellow]")

        log.write("")
        self.push_screen(ModelSelectionScreen(ENV), self._on_model_choice)

    def _on_model_choice(self, model_id):
        if model_id is None: return
        self.selected_model = model_id
        s   = STRINGS[self.lang]
        log = self.query_one("#log", GameLog)
        log.write("[green]+ Model: " + model_id + "[/green]\n")

        saves = sorted(SAVE_DIR.glob("*.json"),
                       key=lambda p: p.stat().st_mtime, reverse=True) if SAVE_DIR.exists() else []
        if saves:
            self.push_screen(SaveChargerScreen(saves, s), self._on_save_choice)
        else:
            self.push_screen(CharacterCreationScreen(s, self.lang), self._on_char_done)

    def _on_save_choice(self, save_data):
        s = STRINGS[self.lang]
        if save_data is None:
            self.push_screen(CharacterCreationScreen(s, self.lang), self._on_char_done)
        else:
            self._load_game(save_data)

    def _load_game(self, data):
        s   = STRINGS[self.lang]
        log = self.query_one("#log", GameLog)
        sb  = self.query_one("#stats", StatsBar)

        self.character = Character.from_dict(data["character"])

        st = data.get("stats", {})
        sb.name_str = self.character.name
        sb.playbook = self.character.playbook
        sb.attrs    = self.character.attrs
        sb.vit = st.get("vit",15);  sb.max_vit = st.get("max_vit",15)
        sb.chg = st.get("chg", 8);  sb.max_chg = st.get("max_chg", 8)
        sb.arm = st.get("arm", 0);  sb.marks   = st.get("marks", 0)
        sb._refresh()

        self.gm = Nexus9GM(self.selected_model, self.character,
                            self._rules, self._world, lang=self.lang)
        self.gm.from_dict(data.get("gm", {}))
        sb.heat       = self.gm.heat
        sb.conditions = self.gm.conditions
        sb.credits    = self.gm.credits
        sb.scene      = self.gm.scene_counter
        sb._refresh()

        log.write("[bold cyan]=== NEXUS-9 ===[/bold cyan]")
        log.write("[bold green]" + s["loaded"] + self.character.name + "[/bold green]")
        log.write("[yellow]Heat:" + str(self.gm.heat) + "  Scene:" + str(self.gm.scene_counter) + "[/yellow]")
        log.write("[dim]" + s["resumed"] + "[/dim]\n")

        self.transcript = data.get('transcript', [])
        last_text = data.get('last_text', '')
        if last_text:
            log.write('[dim]— Last scene —[/dim]')
            log.write("[green]" + last_text + "[/green]")
        log.write("\n[bold yellow]" + ("Votre tour." if self.lang == "fr" else "Your turn.") + "[/bold yellow]\n")

    def _on_char_done(self, character):
        if character is None: return
        self.character = character
        self.transcript = []  
        s = STRINGS[self.lang]

        sb = self.query_one("#stats", StatsBar)
        sb.name_str = character.name
        sb.playbook = character.playbook
        sb.attrs    = character.attrs
        sb.scene    = 0
        sb.max_vit  = character.max_vit; sb.vit  = character.max_vit
        sb.max_chg  = character.max_chg; sb.chg  = character.max_chg
        sb.arm      = character.arm;     sb.marks = 0
        sb.credits  = character.credits
        sb._refresh()

        log = self.query_one("#log", GameLog)
        log.write("[bold cyan]=== NEXUS-9 ===[/bold cyan]")
        log.write("[yellow]" + character.name + " -- " + character.playbook + "[/yellow]\n")
        log.write("[green]" + s["gm_connected"] + "[/green]")
        log.write("[dim]" + s["launching"] + "[/dim]\n")

        self.gm = Nexus9GM(self.selected_model, self.character,
                            self._rules, self._world, lang=self.lang)
        self.processing = True
        threading.Thread(target=self._launch_campaign, daemon=True).start()

    def _launch_campaign(self):
        try:
            text, cost, tokens, changes, session_cost = self.gm.launch_campaign()
            self.call_from_thread(self.display_text, text, cost, tokens, changes, session_cost)
        except Exception as e:
            self.call_from_thread(self.query_one("#log", GameLog).write,
                                  "[red]Launch error: " + str(e) + "[/red]")
        finally:
            self.processing = False

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if self.processing: return
        user_input = event.value.strip()
        if not user_input: return
        event.input.value = ""

        log = self.query_one("#log", GameLog)
        sb  = self.query_one("#stats", StatsBar)
        s   = STRINGS[self.lang]
        cmd = user_input.lower()

        if cmd in ("help","?","h","aide"):
            log.write("[bold cyan]" + s["help_title"] + "[/bold cyan]")
            log.write(s["help_save"])
            log.write(s["help_mute"])
            log.write(s["help_sheet"])
            log.write(s["help_stats"] + "\n")
            return

        if cmd == "sheet" and self.character:
            c = self.character
            log.write("\n[bold cyan]" + s["sheet_title"] + "[/bold cyan]")
            log.write("[cyan]" + c.name + " -- " + c.playbook + " (" + c.background + ")[/cyan]")
            log.write("[dim]" + s["sheet_attributes"] + "[/dim]")
            for attr in ["Grit","Reflex","Mind","Face"]:
                if attr in c.attrs:
                    log.write("  " + _attr_label(self.lang, attr) + ": d" + str(c.attrs[attr]))
            log.write("[dim]" + s["sheet_skills"] + "[/dim]")
            for tier in ["d12","d10","d8","d6","d4"]:
                if c.skills.get(tier):
                    log.write("  " + tier + ": " + ", ".join(_skill_label(self.lang, sk) for sk in c.skills[tier]))
            log.write("[dim]VIT:[/dim] " + str(c.max_vit) + "  [dim]CHG:[/dim] " + str(c.max_chg) + "  [dim]ARM:[/dim] " + str(c.arm) + "  [dim]CRD:[/dim] " + str(self.gm.credits if self.gm else c.credits))
            if c.gear:
                log.write("[dim]Gear:[/dim] " + ", ".join(c.gear))
            log.write("[dim]" + s["label_anchor"] + "[/dim] " + c.anchor)
            log.write("[dim]" + s["label_scar"] + "[/dim] " + c.scar)
            pb = PLAYBOOKS.get(c.playbook, {})
            if pb.get("edge"):
                log.write("[dim]" + s["label_edge"] + "[/dim] " + pb["edge"][self.lang])
            log.write("")
            return

        for stat in ("vit","chg","arm","marks","heat","creds"):
            if cmd.startswith(stat) and len(cmd) > len(stat):
                rest = cmd[len(stat):].strip()
                if rest and (rest[0] in "+-" or rest[0].isdigit()):
                    try:
                        value = int(rest)
                        if stat == "vit":     sb.vit   = max(0, min(sb.max_vit,  sb.vit   + value))
                        elif stat == "chg":   sb.chg   = max(0, min(sb.max_chg,  sb.chg   + value))
                        elif stat == "arm":   sb.arm   = max(0, sb.arm + value)
                        elif stat == "marks": sb.marks = max(0, sb.marks + value)
                        elif stat == "heat" and self.gm:
                            self.gm.heat = max(0, self.gm.heat + value)
                        elif stat == "creds" and self.gm:
                            self.gm.credits = max(0, self.gm.credits + value)
                        sb._refresh()
                        if stat == "heat":    cur = self.gm.heat if self.gm else 0
                        elif stat == "creds": cur = self.gm.credits if self.gm else 0
                        else:                 cur = getattr(sb, stat)
                        col = "red" if value < 0 else "green"
                        log.write("[bold " + col + "]" + stat.upper() + ": " + str(cur) + "[/bold " + col + "]")
                    except ValueError:
                        log.write("[red]" + stat + " +/-N[/red]")
                    return

        if not self.gm:
            log.write("[red]" + s["no_campaign"] + "[/red]")
            return

        self.transcript.append("> " + user_input)
        log.write("\n[bold cyan]> " + user_input + "[/bold cyan]")
        log.write("[dim]" + s["processing"] + "[/dim]")
        self.processing = True
        threading.Thread(target=self._get_response, args=(user_input,), daemon=True).start()

    def _get_response(self, user_input):
        try:
            text, cost, tokens, changes, session_cost = self.gm.send(user_input)
            self.call_from_thread(self.display_text, text, cost, tokens, changes, session_cost)
        except Exception as e:
            self.call_from_thread(self.query_one("#log", GameLog).write,
                                  "[bold red]Error: " + str(e) + "[/bold red]")
        finally:
            self.processing = False

    def display_text(self, text, cost=0.0, tokens=0, changes=None, session_cost=0.0):
        if changes is None: changes = {}
        log = self.query_one("#log", GameLog)
        sb  = self.query_one("#stats", StatsBar)

        log.write("\n[green]" + text + "[/green]\n")
        self.transcript.append(text)
        log.write("[dim]Tokens: " + str(tokens) + " | Cost: $" + "{:.4f}".format(cost) + " | Session: $" + "{:.4f}".format(session_cost) + " | Heat: " + str(self.gm.heat) + "[/dim]\n")

        if "VIT"   in changes: sb.vit   = max(0, min(sb.max_vit,  sb.vit   + changes["VIT"]))
        if "CHG"   in changes: sb.chg   = max(0, min(sb.max_chg,  sb.chg   + changes["CHG"]))
        if "ARM"   in changes: sb.arm   = max(0, sb.arm   + changes["ARM"])
        if "MARKS" in changes: sb.marks = max(0, sb.marks + changes["MARKS"])
        sb.heat       = self.gm.heat
        sb.conditions = self.gm.conditions
        sb.credits    = self.gm.credits
        sb.scene      = self.gm.scene_counter
        sb._refresh()

        if self.tts and self.tts.last_error:
            log.write("[red][TTS] " + self.tts.last_error + "[/red]")
            self.tts.last_error = None

        if self.tts and self.tts.enabled:
            threading.Thread(target=self._parse_and_speak, args=(text,), daemon=True).start()

    def _parse_and_speak(self, text):
        if not (self.tts and self.tts.enabled):
            return

        tasks = []

        for raw in str(text).splitlines():
            line = raw.strip()
            if not line:
                continue

            dm = re.match(r'^\((M|F)(?:,\s*(\w+))?(?:,\s*(\w+))?\)\s*([^:]{1,40})\s*:\s*"(.*)"\s*$', line)
            if dm:
                gender   = "male" if dm.group(1) == "M" else "female"
                emotion  = dm.group(2).strip().lower() if dm.group(2) else None
                age      = dm.group(3).strip().lower() if dm.group(3) else None
                npc_name = dm.group(4).strip()
                spoken   = dm.group(5).strip()
                tasks.append((spoken, gender, npc_name, emotion, age))
                continue

            line2 = re.sub(r'\[(Heat|VIT|CHG|ARM|Marks|Creds)\s*[+-]\s*\d+\]', '', line, flags=re.I)
            line2 = re.sub(r'\[[+-](?:Bruised|Bleeding|Shaken|Crippled|Critical)\]', '', line2)
            line2 = re.sub(r'\[Gear\s*[+-]\s*.+?\]', '', line2).strip()
            if line2:
                tasks.append((line2, "narrator", None, None, None))

        if not tasks:
            return

        import time # Imported here so you don't have to scroll to the top

        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
            futures = [executor.submit(self.tts.synthesize_to_file, *t) for t in tasks]
            
            for future in futures:
                tmp_file = future.result()
                if tmp_file:
                    # Play the audio file
                    subprocess.run(["aplay", "-D", SPEAKER_DEVICE, "-q", tmp_file], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    os.unlink(tmp_file)
                    
                    # Add a natural half-second pause before the next paragraph starts
                    time.sleep(0.5)

    def action_save(self) -> None:
        s = STRINGS[self.lang]
        if not self.gm or not self.character:
            self.query_one("#log", GameLog).write("[yellow]" + s["nothing_to_save"] + "[/yellow]")
            return

        log = self.query_one("#log", GameLog)
        sb  = self.query_one("#stats", StatsBar)
        SAVE_DIR.mkdir(exist_ok=True)

        save_data = {
            "version":   1,
            "saved_at":  datetime.now().isoformat(),
            "character": self.character.to_dict(),
            "stats":     {"vit": sb.vit, "max_vit": sb.max_vit,
                          "chg": sb.chg, "max_chg": sb.max_chg,
                          "arm": sb.arm, "marks":   sb.marks},
            "gm":        self.gm.to_dict(),
            "transcript": self.transcript[-200:],
            "last_text": (self.transcript[-1] if self.transcript else ""),
        }

        slug = re.sub(r'[^a-z0-9_]', '_', self.character.name.lower())
        path = SAVE_DIR / (slug + ".json")
        try:
            path.write_text(json.dumps(save_data, indent=2), encoding="utf-8")
            log.write("[bold green]" + s["saved"] + path.name + "[/bold green]")
        except Exception as e:
            log.write("[bold red]Save failed: " + str(e) + "[/bold red]")

    def action_toggle_tts(self) -> None:
        if self.tts:
            self.tts.toggle()
            self.query_one("#stats", StatsBar).update_stats(tts_on=self.tts.enabled)

    def action_quit(self) -> None:
        self.exit()

if __name__ == "__main__":
    Nexus9App().run()
