import asyncio
import datetime
import io
import os
import random
import time
import sqlite3
import discord
from discord import app_commands, ui
from PIL import Image, ImageDraw, ImageFont, ImageOps
from discord.ext import commands

# ==========================================
# 1. CONFIGURATION INITIALE & CONSTANTES
# ==========================================

TOKEN = os.getenv("DISCORD_TOKEN")
MAX_BET = 500  # Mise maximale autorisée pour les jeux

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.presences = True

bot = commands.Bot(command_prefix="!", intents=intents)

# Cooldowns en mémoire : {(user_id, command_name): timestamp_expiration}
cooldowns = {}
TEST_MODE_ENABLED = False
DB_PATH = "/data/economy.db"


# ==========================================
# 2. GESTIONNAIRE D'ANIMATION DE MESSAGE
# ==========================================

class AnimatedMessageManager:
    def __init__(self, interaction: discord.Interaction, show_animation: bool = True):
        self.interaction = interaction
        self.show_animation = show_animation
        self.last_content = None
        self.last_embed = None

    async def update_animation(self, new_content: str = None, new_embed: discord.Embed = None, view: ui.View = None):
        if not self.show_animation:
            return

        if new_content != self.last_content or new_embed != self.last_embed:
            try:
                await self.interaction.edit_original_response(content=new_content, embed=new_embed, view=view)
                self.last_content = new_content
                self.last_embed = new_embed
            except discord.HTTPException as e:
                if e.status == 429:
                    await asyncio.sleep(1)


# ==========================================
# 3. GESTION DE LA BASE DE DONNÉES (SQLite Persistant)
# ==========================================

def get_db_connection():
    return sqlite3.connect(DB_PATH)


def init_db():
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                wallet INTEGER DEFAULT 0,
                bank INTEGER DEFAULT 0,
                last_daily INTEGER DEFAULT 0,
                streak INTEGER DEFAULT 0,
                beers_today INTEGER DEFAULT 0,
                last_beer_date TEXT DEFAULT '',
                games_played INTEGER DEFAULT 0,
                games_won INTEGER DEFAULT 0,
                games_lost INTEGER DEFAULT 0
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS inventory (
                user_id INTEGER,
                item_name TEXT,
                quantity INTEGER DEFAULT 1,
                PRIMARY KEY (user_id, item_name)
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS shop_items (
                item_key TEXT PRIMARY KEY,
                name TEXT,
                price INTEGER,
                description TEXT,
                shop_type TEXT DEFAULT 'normal',
                episode INTEGER DEFAULT 0,
                required_role_id INTEGER DEFAULT NULL,
                role_to_give_id INTEGER DEFAULT NULL
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS ai_channels (
                guild_id INTEGER,
                ai_type TEXT,
                channel_id INTEGER,
                PRIMARY KEY (guild_id, ai_type)
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS user_preferences (
                user_id INTEGER PRIMARY KEY,
                show_animations INTEGER DEFAULT 1
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS daily_quests (
                user_id INTEGER,
                quest_date TEXT,
                quest_key TEXT,
                description TEXT,
                target INTEGER,
                progress INTEGER DEFAULT 0,
                reward INTEGER,
                claimed INTEGER DEFAULT 0,
                PRIMARY KEY (user_id, quest_date, quest_key)
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS user_achievements (
                user_id INTEGER,
                achievement_key TEXT,
                tier INTEGER DEFAULT 1,
                unlocked_at TEXT,
                PRIMARY KEY (user_id, achievement_key)
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS story_progress (
                user_id INTEGER,
                episode_id INTEGER,
                unlocked_at TEXT,
                PRIMARY KEY (user_id, episode_id)
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS quest_reward_state (
                user_id INTEGER PRIMARY KEY,
                base_reward INTEGER DEFAULT 0,
                quest_date TEXT DEFAULT '',
                quest_streak INTEGER DEFAULT 0,
                last_claim_date TEXT DEFAULT ''
            )
        """)

        # Mises à jour de colonnes si absentes
        columns_to_add = [
            ("last_daily", "INTEGER DEFAULT 0"),
            ("streak", "INTEGER DEFAULT 0"),
            ("beers_today", "INTEGER DEFAULT 0"),
            ("last_beer_date", "TEXT DEFAULT ''"),
            ("games_played", "INTEGER DEFAULT 0"),
            ("games_won", "INTEGER DEFAULT 0"),
            ("games_lost", "INTEGER DEFAULT 0"),
        ]
        for col, col_type in columns_to_add:
            try:
                cursor.execute(f"ALTER TABLE users ADD COLUMN {col} {col_type}")
            except Exception:
                pass

        cursor.execute("PRAGMA table_info(shop_items)")
        shop_cols = [column[1] for column in cursor.fetchall()]
        if "shop_type" not in shop_cols:
            cursor.execute("ALTER TABLE shop_items ADD COLUMN shop_type TEXT DEFAULT 'normal'")
        if "episode" not in shop_cols:
            cursor.execute("ALTER TABLE shop_items ADD COLUMN episode INTEGER DEFAULT 0")
        if "required_role_id" not in shop_cols:
            cursor.execute("ALTER TABLE shop_items ADD COLUMN required_role_id INTEGER DEFAULT NULL")
        if "role_to_give_id" not in shop_cols:
            cursor.execute("ALTER TABLE shop_items ADD COLUMN role_to_give_id INTEGER DEFAULT NULL")

        cursor.execute("SELECT COUNT(*) FROM shop_items")
        if cursor.fetchone()[0] == 0:
            default_items = [
                ("A1", "👑 Rôle VIP", 5000, "Un statut de VIP sur le serveur.", "normal", 0, None, None),
                ("A2", "🎁 Boîte Mystère", 1000, "Contient une surprise aléatoire !", "normal", 0, None, None),
                ("SP1", "💎 Épée Légendaire", 25000, "Une arme surpuissante réservée aux VIP.", "special", 0, None, None)
            ]
            cursor.executemany("INSERT INTO shop_items (item_key, name, price, description, shop_type, episode, required_role_id, role_to_give_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", default_items)

        cursor.execute("DELETE FROM shop_items WHERE shop_type = 'episode'")
        episode_items = []
        for ep in range(1, 26):
            episode_items.extend([
                (f"EP{ep}_1", f"Relique Alpha [0{ep:02d}]" if ep < 10 else f"Relique Alpha [{ep}]", 500, "Objet d'histoire essentiel.", "episode", ep, None, None),
                (f"EP{ep}_2", f"Relique Bêta [0{ep:02d}]" if ep < 10 else f"Relique Bêta [{ep}]", 500, "Objet d'histoire essentiel.", "episode", ep, None, None),
                (f"EP{ep}_3", f"Relique Gamma [0{ep:02d}]" if ep < 10 else f"Relique Gamma [{ep}]", 500, "Objet d'histoire essentiel.", "episode", ep, None, None),
                (f"EP{ep}_4", f"Relique Delta [0{ep:02d}]" if ep < 10 else f"Relique Delta [{ep}]", 500, "Objet d'histoire essentiel.", "episode", ep, None, None),
            ])
        cursor.executemany("INSERT INTO shop_items (item_key, name, price, description, shop_type, episode, required_role_id, role_to_give_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", episode_items)
        conn.commit()


def format_currency(amount: int) -> str:
    return f"{amount:,} $".replace(",", " ")


def get_user_animation_preference(user_id: int) -> bool:
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT show_animations FROM user_preferences WHERE user_id = ?", (user_id,))
        row = cursor.fetchone()
        if row is None:
            cursor.execute("INSERT OR IGNORE INTO user_preferences (user_id, show_animations) VALUES (?, 1)", (user_id,))
            conn.commit()
            return True
        return bool(row[0])


def set_user_animation_preference(user_id: int, show: bool):
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("INSERT OR REPLACE INTO user_preferences (user_id, show_animations) VALUES (?, ?)", (user_id, 1 if show else 0))
        conn.commit()


def get_user(user_id: int):
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT wallet, bank, last_daily, streak, beers_today, last_beer_date, games_played, games_won, games_lost FROM users WHERE user_id = ?",
            (user_id,),
        )
        row = cursor.fetchone()
        if row is None:
            cursor.execute(
                "INSERT INTO users (user_id, wallet, bank, last_daily, streak, beers_today, last_beer_date, games_played, games_won, games_lost)"
                " VALUES (?, 100, 0, 0, 0, 0, '', 0, 0, 0)",
                (user_id,),
            )
            conn.commit()
            return 100, 0, 0, 0, 0, '', 0, 0, 0
        return (
            (row[0] or 0),
            (row[1] or 0),
            (row[2] or 0),
            (row[3] or 0),
            (row[4] or 0),
            (row[5] or ''),
            (row[6] or 0),
            (row[7] or 0),
            (row[8] or 0),
        )


def update_wallet(user_id: int, amount: int):
    get_user(user_id)
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE users SET wallet = COALESCE(wallet, 0) + ? WHERE user_id = ?",
            (amount, user_id),
        )
        conn.commit()
    if amount > 0:
        update_quest_progress(user_id, "money_earned", amount)


def update_game_stats(user_id: int, won: bool):
    get_user(user_id)
    with get_db_connection() as conn:
        cursor = conn.cursor()
        if won:
            cursor.execute("UPDATE users SET games_played = COALESCE(games_played, 0) + 1, games_won = COALESCE(games_won, 0) + 1 WHERE user_id = ?", (user_id,))
        else:
            cursor.execute("UPDATE users SET games_played = COALESCE(games_played, 0) + 1, games_lost = COALESCE(games_lost, 0) + 1 WHERE user_id = ?", (user_id,))
        conn.commit()
    update_quest_progress(user_id, "games_played", 1)
    if won:
        update_quest_progress(user_id, "games_won", 1)


# ==========================================
# 3.1. DONNÉES DE L'HISTOIRE & ÉPISODES (GUILLAUME)
# ==========================================

EPISODE_TITLES = {
    1: "Épisode 1 — L’Arche",
    2: "Épisode 2 — Les Terres Tempérées",
    3: "Épisode 3 — Les Premières Villes",
    4: "Épisode 4 — Le Registre des Dirigeants",
    5: "Épisode 5 — Le Premier Départ",
    6: "Épisode 6 — Le Grand Départ",
    7: "Épisode 7 — La Première Capitale",
    8: "Épisode 8 — Le Gardien des Arches",
    9: "Épisode 9 — Le Mystère des Arches",
    10: "Épisode 10 — Les Premières Conquêtes",
    11: "Épisode 11 — La Course aux Territoires",
    12: "Épisode 12 — La Première Décision",
    13: "Épisode 13 — Le Premier Affrontement",
    14: "Épisode 14 — Trop Tard",
    15: "Épisode 15 — Le Temps Joue Contre Toi",
    16: "Épisode 16 — Le Piège",
    17: "Épisode 17 — Seraph",
    18: "Épisode 18 — L’Expédition",
    19: "Épisode 19 — Le Vétéran",
    20: "Épisode 20 — Les Temples",
    21: "Épisode 21 — La Guerre des Temples",
    22: "Épisode 22 — L’Après-Bataille",
    23: "Épisode 23 — Une Réputation naissante",
    24: "Épisode 24 — Le Prix de la Progression",
    25: "Épisode 25 — Le Siège"
}

EPISODE_STORIES = {
    1: (
        "« La journée touchait à sa fin.\n\n"
        "Comme presque tous les soirs, le Voyageur traversait le vieux parc pour rentrer chez lui.\n"
        "Au centre se dressait une immense arche de pierre.\n"
        "Les enfants jouaient autour.\n"
        "Les adultes passaient devant sans même la regarder.\n"
        "Pour eux… Ce n’était qu’une vieille ruine.\n\n"
        "« Mon ballon ! »\n\n"
        "Une petite voix brisa le silence. Un ballon venait de rouler sous l’arche. Sans réfléchir, le Voyageur courut le récupérer. "
        "Il le ramassa, puis fit un pas pour revenir.\n\n"
        "Le vent s’arrêta. Plus un bruit. Il leva lentement les yeux. Le parc avait disparu.\n"
        "À sa place… Une vaste route pavée traversait une immense plaine. Des caravanes avançaient lentement. Des marchands discutaient.\n"
        "Le Voyageur resta figé.\n\n"
        "Parmi les voyageurs, certains ne ressemblaient à aucun être qu’il avait déjà vu. Leurs traits rappelaient ceux de grands félins, "
        "pourtant personne ne semblait leur accorder le moindre regard. Pendant un instant, il se demanda s’il était en train de rêver.\n\n"
        "Des gardes escortaient les convois. Au loin, une immense cité dominait l’horizon. Tout autour, de nombreuses villes s’étendaient à perte de vue. "
        "Presque toutes arboraient une bannière flottant au-dessus de leurs remparts. Certaines laissaient s’élever d’épaisses colonnes de fumée, "
        "signe qu’une bataille venait d’éclater.\n\n"
        "Un marchand le regarda de la tête aux pieds :\n"
        "— Ces vêtements… Tu viens d’une Arche, n’est-ce pas ?\n\n"
        "Le Voyageur n’eut pas le temps de répondre. Une corne de guerre retentit. Tous les regards se tournèrent vers l’horizon.\n"
        "Au loin… Une immense armée avançait vers la cité. Les portes commencèrent à se refermer.\n\n"
        "Le marchand attrapa brusquement le bras du Voyageur :\n"
        "— Si tu veux vivre… ne reste pas ici ! »"
    ),
    2: (
        "« Le Voyageur suivit le vieil homme à través les rues pavées.\n\n"
        "Tout lui semblait étrange. Son regard ne cessait de parcourir la cité.\n"
        "Des marchands installaient leurs étals. Des soldats patrouillaient le long des remparts.\n"
        "Parmi les habitants, certains avaient des traits félins. Ils échangeaient, travaillaient et riaient aux côtés des humains, comme si cela avait toujours été ainsi.\n"
        "Le Voyageur détourna un instant le regard, puis observa de nouveau. Il comprenait peu à peu que ce monde possédait ses propres règles.\n\n"
        "Le vieil homme s'arrêta devant un immense bâtiment de pierre portant l’emblème d’une Arche.\n"
        "— Bienvenue dans les Terres Tempérées. C'est ici que commence le véritable chemin des dirigeants. »"
    ),
    3: "« Les frontières des Terres Tempérées s'étendaient. De nouvelles cités sortaient de terre, et avec elles, la nécessité de marquer son territoire et d'établir de premières alliances durables. »",
    4: "« Le Registre des Dirigeants fut ouvert. Chaque nom, chaque acte posé dans ce monde nouveau était désormais consigné pour l'éternité par les scribes de la cité. »",
    5: "« Le moment était venu de quitter le confort précaire des premières routes pour fonder sa propre base d'opérations. Un premier grand départ vers l'inconnu. »",
    6: "« Les chariots étaient pleins, les provisions comptées. Le Grand Départ marqua la fin des hésitations : la colonisation des terres sauvages pouvait commencer. »",
    7: "« Après des jours de marche et de luttes, la première véritable capitale s'éleva, fière et dominante, au cœur du territoire conquis. »",
    8: "« Les légendes racontaient l'existence d'un Gardien veillant sur les secrets des Arches originelles. Le Voyageur dut prouver sa valeur pour l'approcher. »",
    9: "« Le voile se leva un peu plus sur l'origine des Arches. Des textes anciens révélèrent que ces portails n'étaient pas le fruit du hasard, mais d'une volonté oubliée. »",
    10: "« Les bannières flottaient fièrement. Les premières véritables conquêtes territoriales s'achevèrent par la soumission des avant-postes rivaux. »",
    11: "« La course aux territoires s'accéléra. Chaque clan, chaque dirigeant cherchait à s'emparer des ressources stratégiques avant ses voisins. »",
    12: "« Une décision cruciale dut être prise sur le front. Un choix militaire qui allait déterminer la survie ou la chute de la garnison. »",
    13: "« Le fracas des armes résonna dans la vallée. Le premier affrontement direct scella le destin des forces en présence. »",
    14: "« Il était déjà trop tard pour négocier. Les erreurs de stratégie se payaient au prix fort dans ces contrées impitoyables. »",
    15: "« Le temps jouait contre le Voyageur. Chaque seconde gaspillée rapprochait l'ennemi des portes de la cité. »",
    16: "« Un piège soigneusement tendu faillit anoncer la fin de l'expédition. La prudence devint la seule alliée des survivants. »",
    17: "« L'ombre mystérieuse de Seraph se profilait à l'horizon, apportant avec elle des réponses, mais aussi de nouveaux périls. »",
    18: "« L'expédition s'enfonça dans les zones inexplorées à la recherche de reliques perdues et de technologies d'un autre âge. »",
    19: "« Un vieux vétéran des guerres passées partagea son expérience et ses cicatrices avec le Voyageur, offrant de précieux conseils tactiques. »",
    20: "« Les temples anciens, longtemps endormis, s'éveillèrent un à un, révélant une puissance mystique insoupçonnée. »",
    21: "« La guerre des temples éclata, dressant les factions les unes contre les autres pour le contrôle de ces sanctuaires sacrés. »",
    22: "« Le silence de l'après-bataille laissa place au bilan des pertes et à la réorganisation des forces en vue des prochaines échéances. »",
    23: "« Une réputation naissante précédait désormais le Voyageur à travers tout le royaume, ouvrant de nouvelles portes diplomatiques. »",
    24: "« Le prix de la progression foi élevé, exigeant des sacrifices constants et une gestion rigoureuse des richesses accumulées. »",
    25: "« L'épreuve ultime : Le Siège final. Tout ce qui avait été bâti se retrouva jeté dans la balance pour l'assaut décisif. »"
}


# ==========================================
# 3.2. SYSTÈMES DE QUÊTES & ACHIEVEMENTS
# ==========================================

QUEST_POOL = [
    {"key": "games_played", "label": "🎲 Joueur Assidu", "desc_tpl": "Jouer {target} partie(s) dans un jeu de casino", "target_range": (3, 6), "reward_range": (150, 300)},
    {"key": "games_won", "label": "🏆 Chanceux du Jour", "desc_tpl": "Gagner {target} partie(s) dans n'importe quel jeu", "target_range": (1, 3), "reward_range": (200, 400)},
    {"key": "work_done", "label": "💼 Travailleur", "desc_tpl": "Travailler {target} fois via /work", "target_range": (1, 3), "reward_range": (100, 250)},
    {"key": "arena_fight", "label": "⚔️ Guerrier de l'Arène", "desc_tpl": "Affronter Bob dans l'arène {target} fois", "target_range": (1, 2), "reward_range": (200, 400)},
    {"key": "duel_played", "label": "🤺 Duelliste", "desc_tpl": "Faire {target} duel(s) PvP contre un ami", "target_range": (1, 2), "reward_range": (250, 450)},
    {"key": "bank_deposit", "label": "🏦 Épargnant", "desc_tpl": "Déposer de l'argent à la banque {target} fois", "target_range": (1, 3), "reward_range": (100, 200)},
    {"key": "pay_sent", "label": "💸 Généreux", "desc_tpl": "Envoyer de l'argent à un ami via /pay {target} fois", "target_range": (1, 2), "reward_range": (100, 200)},
    {"key": "crime_attempt", "label": "🥷 Petite Frappe", "desc_tpl": "Tenter ta chance chez John le Brigand {target} fois", "target_range": (1, 3), "reward_range": (150, 300)},
    {"key": "pmu_bet", "label": "🐎 Turfiste", "desc_tpl": "Parier sur une course chez Brook {target} fois", "target_range": (1, 3), "reward_range": (150, 300)},
    {"key": "vault_attempt", "label": "🔐 Braqueur de Coffre", "desc_tpl": "Tenter de braquer le coffre de la Brinks {target} fois", "target_range": (1, 2), "reward_range": (200, 400)},
    {"key": "money_earned", "label": "💰 Homme d'Affaires", "desc_tpl": "Gagner un total de {target} $", "target_range": (500, 1500), "reward_range": (200, 400)},
    {"key": "beer_drunk", "label": "🍺 Bon Vivant", "desc_tpl": "Commander {target} pinte(s) chez Jim", "target_range": (1, 3), "reward_range": (100, 200)},
]


def _today_str() -> str:
    return time.strftime("%Y-%m-%d")


def get_quest_multiplier(quest_streak: int) -> float:
    return max(1.0, min(3.0, 1.0 + (quest_streak * 0.15)))


def get_quest_reward_state(user_id: int):
    today = _today_str()
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT base_reward, quest_date, quest_streak, last_claim_date FROM quest_reward_state WHERE user_id = ?", (user_id,))
        row = cursor.fetchone()
        if row is None:
            base_reward = random.randint(50, 200)
            cursor.execute("INSERT INTO quest_reward_state (user_id, base_reward, quest_date, quest_streak, last_claim_date) VALUES (?, ?, ?, 0, '')", (user_id, base_reward, today))
            conn.commit()
            return base_reward, 0, ''
        base_reward, quest_date, quest_streak, last_claim_date = row
        if quest_date != today:
            base_reward = random.randint(50, 200)
            cursor.execute("UPDATE quest_reward_state SET base_reward = ?, quest_date = ? WHERE user_id = ?", (base_reward, today, user_id))
            conn.commit()
        return base_reward, (quest_streak or 0), (last_claim_date or '')


def get_daily_quests(user_id: int):
    today = _today_str()
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT quest_key, description, target, progress, reward, claimed FROM daily_quests WHERE user_id = ? AND quest_date = ? ORDER BY quest_key", (user_id, today))
        rows = cursor.fetchall()
        if rows:
            return [{"key": r[0], "description": r[1], "target": r[2], "progress": r[3], "reward": r[4], "claimed": bool(r[5])} for r in rows]

        chosen = random.sample(QUEST_POOL, k=min(5, len(QUEST_POOL)))
        quests = []
        for q in chosen:
            target = random.randint(*q["target_range"])
            description = q["desc_tpl"].format(target=target)
            cursor.execute("INSERT OR IGNORE INTO daily_quests (user_id, quest_date, quest_key, description, target, progress, reward, claimed) VALUES (?, ?, ?, ?, ?, 0, 0, 0)", (user_id, today, q["key"], description, target))
            quests.append({"key": q["key"], "description": description, "target": target, "progress": 0, "reward": 0, "claimed": False})
        conn.commit()
        return quests


def update_quest_progress(user_id: int, quest_key: str, amount: int = 1):
    if amount <= 0:
        return
    get_daily_quests(user_id)
    today = _today_str()
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE daily_quests SET progress = MIN(target, progress + ?) WHERE user_id = ? AND quest_date = ? AND quest_key = ? AND claimed = 0", (amount, user_id, today, quest_key))
        conn.commit()


ACHIEVEMENTS_DEFS = {
    "games_master": {"title": "Maître des Jeux", "desc": "Gagner des parties dans les jeux de casino.", "thresholds": {1: 1}, "rewards": {1: 200}},
    "wealth_tycoon": {"title": "Magnat de l'Économie", "desc": "Posséder un patrimoine cumulé.", "thresholds": {1: 1000}, "rewards": {1: 250}},
    "tavern_guest": {"title": "Habitué de la Taverne", "desc": "Commander des pintes chez Jim.", "thresholds": {1: 1}, "rewards": {1: 150}},
    "arena_gladiator": {"title": "Gladiateur de l'Arène", "desc": "Combattre dans l'arène.", "thresholds": {1: 1}, "rewards": {1: 300}},
    "criminal_mind": {"title": "Hors-la-loi", "desc": "Réussir des crimes.", "thresholds": {1: 1}, "rewards": {1: 250}},
    "quest_seeker": {"title": "Aventurier Régulier", "desc": "Réclamer ses quêtes.", "thresholds": {1: 1}, "rewards": {1: 200}}
}


async def generate_mee6_profile_card(member: discord.Member, unlocked_achievements: dict) -> io.BytesIO:
    width, height = 740, 230
    img = Image.new("RGBA", (width, height), (24, 25, 28, 255))
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle([0, 0, width, height], radius=12, fill="#18191C", outline="#F1C40F", width=2)

    avatar_img = None
    try:
        if member.avatar:
            avatar_bytes = await member.avatar.replace(size=128, format="png").read()
            avatar_img = Image.open(io.BytesIO(avatar_bytes)).convert("RGBA").resize((80, 80), Image.Resampling.LANCZOS)
    except Exception:
        pass
    if avatar_img is None:
        avatar_img = Image.new("RGBA", (80, 80), (50, 50, 60, 255))

    mask = Image.new("L", (80, 80), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, 80, 80), fill=255)
    img.paste(avatar_img, (25, 25), mask=mask)

    try:
        font_name = ImageFont.truetype("arialbd.ttf", 22)
        font_sub = ImageFont.truetype("arial.ttf", 13)
        font_header = ImageFont.truetype("arialbd.ttf", 11)
        font_badge_tier = ImageFont.truetype("arialbd.ttf", 10)
    except IOError:
        font_name = font_sub = font_header = font_badge_tier = ImageFont.load_default()

    draw.text((120, 28), member.display_name, fill="#FFFFFF", font=font_name)
    draw.text((120, 58), "• IV • | Membre des Sceaux", fill="#949BA4", font=font_sub)
    draw.text((120, 82), f"Achievements unlocked  {len(unlocked_achievements)} | 6", fill="#B5BAC1", font=font_sub)
    draw.text((25, 118), "ACHIEVEMENTS", fill="#80848E", font=font_header)

    start_x, start_y, spacing, idx = 25, 142, 65, 0
    for ach_key, tier in unlocked_achievements.items():
        if idx >= 9:
            break
        bx, by = start_x + (idx * spacing), start_y
        draw.polygon([(bx + 22, by), (bx + 44, by + 12), (bx + 44, by + 38), (bx + 22, by + 50), (bx, by + 38), (bx, by + 12)], fill="#0F151D", outline="#CD7F32")
        draw.rounded_rectangle([bx + 4, by + 42, bx + 40, by + 58], radius=4, fill="#232428")
        draw.text((bx + 22, by + 50), "BRO", fill="#FFFFFF", font=font_badge_tier, anchor="mm")
        idx += 1

    buffer = io.BytesIO()
    img.save(buffer, format="PNG")
    buffer.seek(0)
    return buffer


def evaluate_stat_for_achievement(key: str, user_id: int) -> int:
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT wallet, bank, beers_today, games_played, games_won FROM users WHERE user_id = ?", (user_id,))
        row = cursor.fetchone()
        if not row:
            return 0
        wallet, bank, beers_today, games_played, games_won = row
        if key == "games_master": return (games_won or 0)
        elif key == "wealth_tycoon": return (wallet or 0) + (bank or 0)
        elif key == "tavern_guest": return (beers_today or 0)
        elif key == "arena_gladiator" or key == "criminal_mind": return (games_played or 0)
        elif key == "quest_seeker":
            cursor.execute("SELECT COUNT(*) FROM daily_quests WHERE user_id = ? AND claimed = 1", (user_id,))
            q_row = cursor.fetchone()
            return q_row[0] if q_row else 0
    return 0


async def check_and_unlock_achievements(user_id: int, bot_client=None) -> list:
    today = time.strftime("%Y-%m-%d")
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT achievement_key FROM user_achievements WHERE user_id = ?", (user_id,))
        user_unlocked = {row[0] for row in cursor.fetchall()}

    for key, data in ACHIEVEMENTS_DEFS.items():
        if key in user_unlocked:
            continue
        if evaluate_stat_for_achievement(key, user_id) >= data["thresholds"][1]:
            update_wallet(user_id, data["rewards"][1])
            with get_db_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("INSERT OR REPLACE INTO user_achievements (user_id, achievement_key, tier, unlocked_at) VALUES (?, ?, 1, ?)", (user_id, key, today))
                conn.commit()

            if bot_client:
                try:
                    with get_db_connection() as db_conn:
                        cur = db_conn.cursor()
                        cur.execute("SELECT channel_id FROM ai_channels WHERE ai_type = ?", ("achievements",))
                        ch_row = cur.fetchone()
                        if ch_row:
                            target_channel = bot_client.get_channel(ch_row[0])
                            if target_channel:
                                member_obj = target_channel.guild.get_member(user_id)
                                if member_obj:
                                    img_buf = await generate_mee6_profile_card(member_obj, {key: 1})
                                    file = discord.File(fp=img_buf, filename="achievement.png")
                                    await target_channel.send(content=f"🏆 GG <@{user_id}>, tu as débloqué le succès **{data['title']}** !", file=file)
                except Exception as e:
                    print(f"Erreur notification succès: {e}")
            break
    return []


# ==========================================
# 4. HELPERS DE JEU & COOLDOWNS
# ==========================================

def check_cooldown(user_id: int, command_name: str, duration: int) -> int:
    if TEST_MODE_ENABLED:
        return 0
    now = int(time.time())
    expire = cooldowns.get((user_id, command_name), 0)
    if now < expire:
        return expire - now
    cooldowns[(user_id, command_name)] = now + duration
    return 0


async def validate_game_bet(interaction: discord.Interaction, command_name: str, bet: int, cooldown_sec: int = 3600) -> bool:
    if bet <= 0 or bet > MAX_BET:
        await interaction.response.send_message(f"❌ La mise doit être entre 1 $ et {MAX_BET} $ !", ephemeral=True)
        return False
    wallet, _, _, _, _, _, _, _, _ = get_user(interaction.user.id)
    if wallet < bet:
        await interaction.response.send_message("❌ Solde insuffisant dans votre portefeuille !", ephemeral=True)
        return False
    retry_after = check_cooldown(interaction.user.id, command_name, cooldown_sec)
    if retry_after > 0:
        m, s = divmod(retry_after, 60)
        await interaction.response.send_message(f"⏳ Attendez **{m}m {s}s** avant de rejouer.", ephemeral=True)
        return False
    return True


# ==========================================
# 5. MINI-JEUX DE CASINO (DÉS, ROULETTE, BJ, SLOTS, PMU, ARÈNE)
# ==========================================

async def run_dice_game(interaction: discord.Interaction, bet: int):
    if not await validate_game_bet(interaction, "dice", bet, 10):
        return
    user_id = interaction.user.id
    update_wallet(user_id, -bet)
    player_roll = random.randint(1, 6)
    bot_roll = random.randint(1, 6)
    
    if player_roll > bot_roll:
        winnings = bet * 2
        update_wallet(user_id, winnings)
        update_game_stats(user_id, True)
        msg = f"🎲 Tu as fait **{player_roll}**, le bot a fait **{bot_roll}**. **Victoire !** +{format_currency(winnings)}"
    elif player_roll < bot_roll:
        update_game_stats(user_id, false:=False)
        msg = f"🎲 Tu as fait **{player_roll}**, le bot a fait **{bot_roll}**. **Défaite !** -{format_currency(bet)}"
    else:
        update_wallet(user_id, bet)
        msg = f"🎲 Égalité (**{player_roll}** partout). Mise remboursée !"
    
    await interaction.followup.send(msg, ephemeral=True)
    await check_and_unlock_achievements(user_id, bot_client=bot)


async def run_roulette_game(interaction: discord.Interaction, bet: int):
    if not await validate_game_bet(interaction, "roulette", bet, 10):
        return
    user_id = interaction.user.id
    update_wallet(user_id, -bet)
    win = random.choice([True, False, False]) # Avantage maison
    
    if win:
        winnings = bet * 2
        update_wallet(user_id, winnings)
        update_game_stats(user_id, True)
        await interaction.followup.send(f"🎡 La roulette s'arrête sur la bonne case ! **Victoire !** +{format_currency(winnings)}", ephemeral=True)
    else:
        update_game_stats(user_id, False)
        await interaction.followup.send(f"🎡 La roulette a tourné du mauvais côté. **Perdu !** -{format_currency(bet)}", ephemeral=True)
    await check_and_unlock_achievements(user_id, bot_client=bot)


async def run_blackjack_game(interaction: discord.Interaction, bet: int):
    if not await validate_game_bet(interaction, "blackjack", bet, 15):
        return
    user_id = interaction.user.id
    update_wallet(user_id, -bet)
    player_score = random.randint(17, 21)
    dealer_score = random.randint(15, 21)
    
    if player_score > 21 or (dealer_score >= player_score and dealer_score <= 21):
        update_game_stats(user_id, False)
        await interaction.followup.send(f"👑 Blackjack : Tu as {player_score}, le croupier a {dealer_score}. **Perdu !**", ephemeral=True)
    else:
        winnings = int(bet * 1.5)
        update_wallet(user_id, bet + winnings)
        update_game_stats(user_id, True)
        await interaction.followup.send(f"👑 Blackjack : Tu as {player_score}, le croupier a {dealer_score}. **Victoire !** +{format_currency(winnings)}", ephemeral=True)
    await check_and_unlock_achievements(user_id, bot_client=bot)


async def run_slots_game(interaction: discord.Interaction, bet: int):
    if not await validate_game_bet(interaction, "slots", bet, 10):
        return
    user_id = interaction.user.id
    update_wallet(user_id, -bet)
    emojis = ["🍒", "🍋", "🔔", "💎", "7️⃣"]
    res = [random.choice(emojis) for _ in range(3)]
    
    if res[0] == res[1] == res[2]:
        multiplier = 10 if res[0] == "7️⃣" else 5
        winnings = bet * multiplier
        update_wallet(user_id, winnings)
        update_game_stats(user_id, True)
        await interaction.followup.send(f"🪙 {' '.join(res)} — **JACKPOT !** +{format_currency(winnings)}", ephemeral=True)
    elif res[0] == res[1] or res[1] == res[2] or res[0] == res[2]:
        winnings = bet * 2
        update_wallet(user_id, winnings)
        update_game_stats(user_id, True)
        await interaction.followup.send(f"🪙 {' '.join(res)} — **Petit gain !** +{format_currency(winnings)}", ephemeral=True)
    else:
        update_game_stats(user_id, False)
        await interaction.followup.send(f"🪙 {' '.join(res)} — **Rien du tout !** -{format_currency(bet)}", ephemeral=True)
    await check_and_unlock_achievements(user_id, bot_client=bot)


async def run_pmu_game(interaction: discord.Interaction, choice: int, bet: int):
    if not await validate_game_bet(interaction, "pmu", bet, 20):
        return
    user_id = interaction.user.id
    update_wallet(user_id, -bet)
    winner = random.randint(1, 4)
    
    if choice == winner:
        winnings = bet * 3
        update_wallet(user_id, winnings)
        update_game_stats(user_id, True)
        await interaction.followup.send(f"🏁 Le cheval #{winner} a gagné ! C'était ton choix. **Victoire !** +{format_currency(winnings)}", ephemeral=True)
    else:
        update_game_stats(user_id, False)
        await interaction.followup.send(f"🏁 Le cheval #{winner} a gagné (tu avais choisi le #{choice}). **Perdu !**", ephemeral=True)
    await check_and_unlock_achievements(user_id, bot_client=bot)


async def run_brook_pmu_game(interaction: discord.Interaction, choice: int, bet: int, odds: dict):
    await run_pmu_game(interaction, choice, bet)


async def run_arena_fight(interaction: discord.Interaction, bet: int):
    if not await validate_game_bet(interaction, "arena", bet, 30):
        return
    user_id = interaction.user.id
    update_wallet(user_id, -bet)
    win = random.choice([True, False])
    
    if win:
        winnings = bet * 2
        update_wallet(user_id, winnings)
        update_game_stats(user_id, True)
        await interaction.followup.send(f"⚔️ Tu as terrassé Bob dans l'arène ! **Victoire !** +{format_currency(winnings)}", ephemeral=True)
    else:
        update_game_stats(user_id, False)
        await interaction.followup.send(f"⚔️ Bob t'a mis KO dans l'arène... **Défaite !** -{format_currency(bet)}", ephemeral=True)
    await check_and_unlock_achievements(user_id, bot_client=bot)


# ==========================================
# 6. MODALES ET INTERFACES DE BANQUE & PNJ
# ==========================================

class BetModal(ui.Modal):
    def __init__(self, title_name: str, callback_game):
        super().__init__(title=title_name)
        self.callback_game = callback_game
        self.bet_input = ui.TextInput(label="Montant de la mise", placeholder=f"Max: {MAX_BET}$", required=True, max_length=6)
        self.add_item(self.bet_input)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            val = int(self.bet_input.value)
        except ValueError:
            return await interaction.followup.send("❌ Nombre invalide.", ephemeral=True)
        await self.callback_game(interaction, val)


class DepositModal(ui.Modal, title="📥 DAB - Dépôt de billets"):
    amount = ui.TextInput(label="Montant à déposer", placeholder="Ex: 500", required=True)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            val = int(self.amount.value)
        except ValueError:
            return await interaction.followup.send("❌ Montant invalide.", ephemeral=True)
        wallet, _, _, _ = get_user(interaction.user.id)[:4]
        if wallet < val:
            return await interaction.followup.send("❌ Portefeuille insuffisant.", ephemeral=True)
        with get_db_connection() as conn:
            conn.cursor().execute("UPDATE users SET wallet = wallet - ?, bank = bank + ? WHERE user_id = ?", (val, val, interaction.user.id))
            conn.commit()
        update_quest_progress(interaction.user.id, "bank_deposit", 1)
        await check_and_unlock_achievements(interaction.user.id, bot_client=bot)
        await interaction.followup.send(f"💵 Dépôt de **{format_currency(val)}** effectué !", ephemeral=True)


class WithdrawModal(ui.Modal, title="📤 DAB - Retrait de billets"):
    amount = ui.TextInput(label="Montant à retirer", placeholder="Ex: 500", required=True)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            val = int(self.amount.value)
        except ValueError:
            return await interaction.followup.send("❌ Montant invalide.", ephemeral=True)
        _, bank, _, _ = get_user(interaction.user.id)[:4]
        if bank < val:
            return await interaction.followup.send("❌ Solde bancaire insuffisant.", ephemeral=True)
        with get_db_connection() as conn:
            conn.cursor().execute("UPDATE users SET bank = bank - ?, wallet = wallet + ? WHERE user_id = ?", (val, val, interaction.user.id))
            conn.commit()
        await interaction.followup.send(f"💸 Retrait de **{format_currency(val)}** effectué !", ephemeral=True)


class BankView(ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @ui.button(label="[ 💳 SOLDE ]", style=discord.ButtonStyle.primary, custom_id="persistent_bank:solde")
    async def check_balance(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.defer(ephemeral=True)
        wallet, bank, _, _ = get_user(interaction.user.id)[:4]
        embed = discord.Embed(title="💳 RELEVÉ BANCAIRE", description=f"• Portefeuille : **{format_currency(wallet)}**\n• Banque : **{format_currency(bank)}**\n• Total : **{format_currency(wallet + bank)}**", color=0x2B2D31)
        await interaction.followup.send(embed=embed, ephemeral=True)

    @ui.button(label="[ 📥 DÉPÔT ]", style=discord.ButtonStyle.success, custom_id="persistent_bank:depot")
    async def deposit(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.send_modal(DepositModal())

    @ui.button(label="[ 📤 RETRAIT ]", style=discord.ButtonStyle.danger, custom_id="persistent_bank:retrait")
    async def withdraw(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.send_modal(WithdrawModal())


class TavernierGamesView(ui.View):
    def __init__(self):
        super().__init__(timeout=60)

    @ui.button(label="🎲 Dés", style=discord.ButtonStyle.primary, custom_id="taverne_game_dice")
    async def play_dice(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.send_modal(BetModal("🎲 Dés - Mise", run_dice_game))

    @ui.button(label="🎡 Roulette", style=discord.ButtonStyle.primary, custom_id="taverne_game_roulette")
    async def play_roulette(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.send_modal(BetModal("🎡 Roulette - Mise", run_roulette_game))

    @ui.button(label="👑 Blackjack", style=discord.ButtonStyle.success, custom_id="taverne_game_bj")
    async def play_bj(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.send_modal(BetModal("👑 Blackjack - Mise", run_blackjack_game))

    @ui.button(label="🪙 Slots", style=discord.ButtonStyle.success, custom_id="taverne_game_slots")
    async def play_slots(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.send_modal(BetModal("🪙 Slots - Mise", run_slots_game))


class JimTavernView(ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @ui.button(label="Commander une Pinte", style=discord.ButtonStyle.primary, emoji="🍺", custom_id="jim_pinte")
    async def pinte(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.defer(ephemeral=True)
        wallet = get_user(interaction.user.id)[0]
        if wallet < 50:
            return await interaction.followup.send("🍺 Jim : \"Tu n'as pas assez pour une pinte !\"", ephemeral=True)
        update_wallet(interaction.user.id, -50)
        update_quest_progress(interaction.user.id, "beer_drunk", 1)
        await check_and_unlock_achievements(interaction.user.id, bot_client=bot)
        await interaction.followup.send("🍻 Jim te sert une pinte bien fraîche. Santé !", ephemeral=True)

    @ui.button(label="Jeux de la Taverne", style=discord.ButtonStyle.success, emoji="🎲", custom_id="jim_games")
    async def games_hub(self, interaction: discord.Interaction, button: ui.Button):
        embed = discord.Embed(title="🎲 Coin des Jeux", description="Choisis un jeu :", color=discord.Color.dark_orange())
        await interaction.response.send_message(embed=embed, view=TavernierGamesView(), ephemeral=True)


class JohnCrimeView(ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @ui.button(label="Tenter un Crime", style=discord.ButtonStyle.danger, emoji="🥷", custom_id="john_crime_btn")
    async def crime_btn(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.defer(ephemeral=True)
        user_id = interaction.user.id
        if check_cooldown(user_id, "john_crime", 1800) > 0:
            return await interaction.followup.send("🥷 John : \"Patiente un peu avant de replonger.\"", ephemeral=True)
        update_quest_progress(user_id, "crime_attempt", 1)
        if random.choice([True, False]):
            gain = random.randint(300, 1000)
            update_wallet(user_id, gain)
            await check_and_unlock_achievements(user_id, bot_client=bot)
            await interaction.followup.send(f"🥷 Vol réussi ! +**{format_currency(gain)}**", ephemeral=True)
        else:
            await interaction.followup.send("🚨 La milice t'a repéré ! Échec.", ephemeral=True)


class BobArenaView(ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @ui.button(label="Entrer dans l'Arène", style=discord.ButtonStyle.danger, emoji="⚔️", custom_id="bob_arena_fight")
    async def fight_btn(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.send_modal(BetModal("⚔️ Arène - Mise de Combat", run_arena_fight))


# ==========================================
# 7. BOUTIQUE & TROUBADOUR (GUILLAUME & TOM)
# ==========================================

class TroubadourPaginationView(ui.View):
    def __init__(self, member: discord.Member, current_ep: int = 1):
        super().__init__(timeout=120)
        self.member, self.current_ep = member, current_ep
        self.update_components()

    def update_components(self):
        self.clear_items()
        prev = ui.Button(label="◀️", style=discord.ButtonStyle.secondary, disabled=(self.current_ep <= 1))
        prev.callback = lambda i: self.navigate(i, -1)
        nxt = ui.Button(label="▶️", style=discord.ButtonStyle.secondary, disabled=(self.current_ep >= 25))
        nxt.callback = lambda i: self.navigate(i, 1)
        self.add_item(prev)
        self.add_item(ui.Button(label=f"Ép. {self.current_ep}/25", style=discord.ButtonStyle.blurple, disabled=True))
        self.add_item(nxt)

        listen = ui.Button(label="📖 Lire l'histoire", style=discord.ButtonStyle.success, row=1)
        listen.callback = self.listen_callback
        self.add_item(listen)

    async def navigate(self, interaction: discord.Interaction, delta: int):
        self.current_ep += delta
        self.update_components()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    async def listen_callback(self, interaction: discord.Interaction):
        story = EPISODE_STORIES.get(self.current_ep, "Histoire mystérieuse...")
        await interaction.response.send_message(embed=discord.Embed(title=EPISODE_TITLES[self.current_ep], description=story, color=discord.Color.gold()), ephemeral=True)

    def build_embed(self) -> discord.Embed:
        return discord.Embed(title=f"🪕 Guillaume — {EPISODE_TITLES[self.current_ep]}", description="Utilisez les boutons pour naviguer.", color=discord.Color.purple())


class PersistentTroubadourView(ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @ui.button(label="Parler à Guillaume", style=discord.ButtonStyle.success, emoji="🪕", custom_id="persistent_troubadour_talk_main")
    async def talk(self, interaction: discord.Interaction, button: ui.Button):
        view = TroubadourPaginationView(interaction.user, 1)
        await interaction.response.send_message(embed=view.build_embed(), view=view, ephemeral=True)


class EpisodeShopView(ui.View):
    def __init__(self, member: discord.Member, episode_num: int):
        super().__init__(timeout=120)
        self.member, self.episode_num = member, episode_num
        self.load_items()

    def load_items(self):
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT item_key, name, price FROM shop_items WHERE shop_type = 'episode' AND episode = ?", (self.episode_num,))
            for key, name, price in cursor.fetchall():
                btn = ui.Button(label=f"Acheter {name} ({format_currency(price)})", style=discord.ButtonStyle.success)
                btn.callback = self.make_callback(key, name, price)
                self.add_item(btn)

    def make_callback(self, key, name, price):
        async def cb(interaction: discord.Interaction):
            wallet = get_user(self.member.id)[0]
            if wallet < price:
                return await interaction.response.send_message("❌ Solde insuffisant !", ephemeral=True)
            with get_db_connection() as conn:
                conn.cursor().execute("UPDATE users SET wallet = wallet - ? WHERE user_id = ?", (price, self.member.id))
                conn.cursor().execute("INSERT INTO inventory (user_id, item_name, quantity) VALUES (?, ?, 1) ON CONFLICT(user_id, item_name) DO UPDATE SET quantity = quantity + 1", (self.member.id, name))
                conn.commit()
            await interaction.response.send_message(f"✅ Achat de **{name}** réussi !", ephemeral=True)
        return cb


class DynamicShopView(ui.View):
    def __init__(self, member: discord.Member, shop_type: str):
        super().__init__(timeout=60)
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT item_key, name, price FROM shop_items WHERE shop_type = ?", (shop_type,))
            for key, name, price in cursor.fetchall():
                btn = ui.Button(label=f"{name} ({format_currency(price)})", style=discord.ButtonStyle.success)
                btn.callback = self.make_callback(key, name, price)
                self.add_item(btn)

    def make_callback(self, key, name, price):
        async def cb(interaction: discord.Interaction):
            wallet = get_user(interaction.user.id)[0]
            if wallet < price:
                return await interaction.response.send_message("❌ Solde insuffisant !", ephemeral=True)
            with get_db_connection() as conn:
                conn.cursor().execute("UPDATE users SET wallet = wallet - ? WHERE user_id = ?", (price, interaction.user.id))
                conn.cursor().execute("INSERT INTO inventory (user_id, item_name, quantity) VALUES (?, ?, 1) ON CONFLICT(user_id, item_name) DO UPDATE SET quantity = quantity + 1", (interaction.user.id, name))
                conn.commit()
            await interaction.response.send_message(f"✅ Achat de **{name}** réussi !", ephemeral=True)
        return cb


class ShopDialogueView(ui.View):
    def __init__(self, member: discord.Member):
        super().__init__(timeout=120)
        self.member = member

    @ui.button(label="🛒 Boutique", style=discord.ButtonStyle.primary, emoji="✨", custom_id="shop_dialogue_browse")
    async def browse(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.send_message(embed=discord.Embed(title="🛒 Boutique", color=discord.Color.gold()), view=DynamicShopView(interaction.user, 'normal'), ephemeral=True)

    @ui.button(label="📖 Histoire", style=discord.ButtonStyle.success, emoji="📜", custom_id="shop_dialogue_story")
    async def story(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.send_message(embed=discord.Embed(title="📜 Boutique Histoire", color=discord.Color.dark_teal()), view=EpisodeShopView(interaction.user, 1), ephemeral=True)

    @ui.button(label="🎒 Inventaire", style=discord.ButtonStyle.secondary, emoji="📦", custom_id="shop_dialogue_inventory")
    async def inv(self, interaction: discord.Interaction, button: ui.Button):
        with get_db_connection() as conn:
            rows = conn.cursor().execute("SELECT item_name, quantity FROM inventory WHERE user_id = ?", (interaction.user.id,)).fetchall()
        desc = "\n".join([f"• **{i}** x`{q}`" for i, q in rows]) if rows else "Vide."
        await interaction.response.send_message(embed=discord.Embed(title="🎒 Inventaire", description=desc, color=discord.Color.blue()), ephemeral=True)


class PersistentMerchantView(ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @ui.button(label="Parler au Marchand", style=discord.ButtonStyle.success, emoji="🦊", custom_id="persistent_merchant_talk_main")
    async def talk(self, interaction: discord.Interaction, button: ui.Button):
        view = ShopDialogueView(interaction.user)
        await interaction.response.send_message(embed=discord.Embed(title="🦊 Tom - Marchand", description="Bienvenue !", color=discord.Color.orange()), view=view, ephemeral=True)


# ==========================================
# 8. COMMANDES DU BOT (SLASH COMMANDS)
# ==========================================

@bot.tree.command(name="banque", description="Accéder au DAB")
async def banque(interaction: discord.Interaction):
    await interaction.response.send_message(embed=discord.Embed(title="🏦 Banque", description="Gérez vos avoirs.", color=0x34495E), view=BankView(), ephemeral=True)


@bot.tree.command(name="balance", description="Vérifie ton solde")
async def balance(interaction: discord.Interaction, member: discord.Member = None):
    target = member or interaction.user
    wallet, bank = get_user(target.id)[:2]
    embed = discord.Embed(title=f"Portefeuille de {target.display_name}", color=discord.Color.blurple())
    embed.add_field(name="Portefeuille", value=format_currency(wallet))
    embed.add_field(name="Banque", value=format_currency(bank))
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="work", description="Gagne de l'argent en travaillant")
async def work(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=False)
    if check_cooldown(interaction.user.id, "work", 3600) > 0:
        return await interaction.followup.send("⏳ Attendez avant de travailler.", ephemeral=True)
    gain = random.randint(100, 500)
    update_wallet(interaction.user.id, gain)
    update_quest_progress(interaction.user.id, "work_done", 1)
    await check_and_unlock_achievements(interaction.user.id, bot_client=bot)
    await interaction.followup.send(f"💼 Vous avez travaillé et gagné **{format_currency(gain)}** !")


@bot.tree.command(name="profile", description="Affiche votre profil")
async def profile(interaction: discord.Interaction):
    u = get_user(interaction.user.id)
    wallet, bank, gp, gw, gl = u[0], u[1], u[6], u[7], u[8]
    embed = discord.Embed(title=f"Profil de {interaction.user.display_name}", color=discord.Color.blurple())
    embed.add_field(name="Finances", value=f"Portefeuille : {format_currency(wallet)}\nBanque : {format_currency(bank)}", inline=False)
    embed.add_field(name="Jeux", value=f"Jouées : {gp} | Gagnées : {gw} | Perdues : {gl}", inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="setup", description="[ADMIN] Configure les salons PNJ")
@app_commands.checks.has_permissions(administrator=True)
async def setup(interaction: discord.Interaction, ai_type: str, salon: discord.TextChannel):
    # Ajout du defer pour éviter l'expiration "L'application ne répond plus"
    await interaction.response.defer(ephemeral=True)
    
    with get_db_connection() as conn:
        conn.cursor().execute("INSERT OR REPLACE INTO ai_channels (guild_id, ai_type, channel_id) VALUES (?, ?, ?)", (interaction.guild.id, ai_type, salon.id))
        conn.commit()
        
    view = JimTavernView() if ai_type == "taverne" else PersistentMerchantView()
    await salon.send(embed=discord.Embed(title=f"Salon PNJ : {ai_type}", color=discord.Color.gold()), view=view)
    await interaction.followup.send("✅ Salon configuré avec succès !", ephemeral=True)


# ==========================================
# 9. LANCEMENT FINAL DU BOT
# ==========================================

@bot.event
async def on_ready():
    init_db()
    bot.add_view(PersistentMerchantView())
    bot.add_view(PersistentTroubadourView())
    bot.add_view(BankView())
    bot.add_view(JimTavernView())
    bot.add_view(JohnCrimeView())
    bot.add_view(BobArenaView())
    print(f"🤖 Bot connecté en tant que {bot.user}")
    try:
        synced = await bot.tree.sync()
        print(f"🌲 {len(synced)} commandes slash synchronisées.")
    except Exception as e:
        print(f"❌ Erreur de sync : {e}")


if __name__ == "__main__":
    bot.run(TOKEN)
