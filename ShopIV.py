import os
import discord
from discord import ui
from discord.ext import commands
import sqlite3

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.presences = True

bot = commands.Bot(command_prefix="!", intents=intents)

# Connexion à la base de données locale SQLite (simple et sans Turso)
DB_PATH = "/data/economy.db"

def get_db_connection():
    return sqlite3.connect(DB_PATH)

# --- Dictionnaire des titres d'épisodes (1 à 25) ---
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

# --- Textes des histoires racontées par Guillaume (1 à 25) ---
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

def get_episode_title(ep_num: int) -> str:
    return EPISODE_TITLES.get(ep_num, f"Épisode {ep_num}")

def format_currency(amount: int) -> str:
    return f"{amount:,} $".replace(",", " ")

def init_db():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            wallet INTEGER DEFAULT 0,
            bank INTEGER DEFAULT 0,
            last_daily INTEGER DEFAULT 0,
            streak INTEGER DEFAULT 0
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
        CREATE TABLE IF NOT EXISTS story_progress (
            user_id INTEGER,
            episode INTEGER,
            PRIMARY KEY (user_id, episode)
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
    
    cursor.execute("PRAGMA table_info(shop_items)")
    columns = [column[1] for column in cursor.fetchall()]
    if "shop_type" not in columns:
        cursor.execute("ALTER TABLE shop_items ADD COLUMN shop_type TEXT DEFAULT 'normal'")
    if "episode" not in columns:
        cursor.execute("ALTER TABLE shop_items ADD COLUMN episode INTEGER DEFAULT 0")
    if "required_role_id" not in columns:
        cursor.execute("ALTER TABLE shop_items ADD COLUMN required_role_id INTEGER DEFAULT NULL")
    if "role_to_give_id" not in columns:
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
    
    # Génération automatique des items d'épisodes 1 à 25
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
    conn.close()

def get_user(user_id: int):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT wallet, bank, last_daily, streak FROM users WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    if not row:
        cursor.execute("INSERT INTO users (user_id, wallet, bank) VALUES (?, 0, 0)", (user_id,))
        conn.commit()
        cursor.execute("SELECT wallet, bank, last_daily, streak FROM users WHERE user_id = ?", (user_id,))
        row = cursor.fetchone()
    conn.close()
    return int(row[0]), int(row[1]), int(row[2]), int(row[3])

@bot.event
async def on_ready():
    init_db()
    bot.add_view(PersistentMerchantView())
    bot.add_view(PersistentTroubadourView())
    print(f"Connecté en tant que {bot.user} (ID: {bot.user.id})")
    try:
        synced = await bot.tree.sync()
        print(f"Synchronisé {len(synced)} commandes slash.")
    except Exception as e:
        print(e)
    print("Prêt !")

# --- SYSTÈME DE GUILLAUME LE TROUBADOUR (INTERFAÇAGE PAGINÉ) ---
class TroubadourPaginationView(ui.View):
    def __init__(self, member: discord.Member, current_ep: int = 1):
        super().__init__(timeout=120)
        self.member = member
        self.current_ep = current_ep
        self.update_components()

    def update_components(self):
        self.clear_items()

        prev_btn = ui.Button(label="◀️ Précédent", style=discord.ButtonStyle.secondary, disabled=(self.current_ep <= 1), row=0)
        prev_btn.callback = self.prev_callback
        self.add_item(prev_btn)

        page_indicator = ui.Button(label=f"Épisode {self.current_ep} / 25", style=discord.ButtonStyle.blurple, disabled=True, row=0)
        self.add_item(page_indicator)

        next_btn = ui.Button(label="Suivant ▶️", style=discord.ButtonStyle.secondary, disabled=(self.current_ep >= 25), row=0)
        next_btn.callback = self.next_callback
        self.add_item(next_btn)

        has_story = self.current_ep in EPISODE_STORIES and bool(EPISODE_STORIES[self.current_ep].strip())

        if not has_story:
            rest_btn = ui.Button(label="💤 Le troubadour se repose, revenez plus tard", style=discord.ButtonStyle.danger, disabled=True, row=1)
            self.add_item(rest_btn)
            return

        user_id = self.member.id
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT 1 FROM story_progress WHERE user_id = ? AND episode = ?", (user_id, self.current_ep))
        is_unlocked = cursor.fetchone() is not None

        if self.current_ep == 1:
            has_all_items = True
        else:
            prev_ep = self.current_ep - 1
            cursor.execute("SELECT name FROM shop_items WHERE shop_type='episode' AND episode=?", (prev_ep,))
            prev_ep_items = [row[0] for row in cursor.fetchall()]

            has_all_items = False
            if prev_ep_items:
                cursor.execute("""
                    SELECT COUNT(*) FROM inventory 
                    WHERE user_id = ? AND item_name IN ({}) AND quantity > 0
                """.format(','.join(['?']*len(prev_ep_items))), [user_id] + prev_ep_items)
                owned_count = cursor.fetchone()[0]
                if owned_count >= len(prev_ep_items):
                    has_all_items = True
        conn.close()

        if is_unlocked:
            listen_btn = ui.Button(label="📖 Écouter / Relire l'histoire", style=discord.ButtonStyle.success, emoji="📜", row=1)
            listen_btn.callback = self.listen_callback
            self.add_item(listen_btn)
        elif self.current_ep == 1:
            listen_btn = ui.Button(label="📖 Écouter l'Histoire (Gratuit)", style=discord.ButtonStyle.success, emoji="📜", row=1)
            listen_btn.callback = self.listen_callback
            self.add_item(listen_btn)
        elif has_all_items:
            give_btn = ui.Button(label="🎁 Donner les reliques & Écouter", style=discord.ButtonStyle.primary, emoji="✨", row=1)
            give_btn.callback = self.give_callback
            self.add_item(give_btn)
        else:
            lock_btn = ui.Button(label="🔒 Épisode Verrouillé (Reliques précédentes manquantes)", style=discord.ButtonStyle.danger, disabled=True, row=1)
            self.add_item(lock_btn)

    async def prev_callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.member.id:
            return await interaction.response.send_message("❌ Ce n'est pas votre tour !", ephemeral=True)
        if self.current_ep > 1:
            self.current_ep -= 1
            self.update_components()
            await interaction.response.edit_message(embed=self.build_embed(), view=self)

    async def next_callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.member.id:
            return await interaction.response.send_message("❌ Ce n'est pas votre tour !", ephemeral=True)
        if self.current_ep < 25:
            self.current_ep += 1
            self.update_components()
            await interaction.response.edit_message(embed=self.build_embed(), view=self)

    async def listen_callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.member.id:
            return await interaction.response.send_message("❌ Ce n'est pas votre tour !", ephemeral=True)
        
        story_text = EPISODE_STORIES.get(self.current_ep, "« Une histoire mystérieuse... »")
        extra_txt = " (Offert à tous les voyageurs !)" if self.current_ep == 1 else " (Vous possédez déjà cet épisode dans vos archives permanentes.)"
        
        embed = discord.Embed(
            title=f"📜 Récit de {get_episode_title(self.current_ep)}",
            description=f"{story_text}\n\n*✨ {extra_txt}*",
            color=discord.Color.gold()
        )
        embed.set_thumbnail(url="https://images.emojiterra.com/google/android-10/512px/1f3ad.png")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    async def give_callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.member.id:
            return await interaction.response.send_message("❌ Ce n'est pas votre tour !", ephemeral=True)
        
        has_story = self.current_ep in EPISODE_STORIES and bool(EPISODE_STORIES[self.current_ep].strip())
        if not has_story:
            return await interaction.response.send_message("❌ Le troubadour se repose, revenez plus tard !", ephemeral=True)

        user_id = self.member.id
        prev_ep = self.current_ep - 1

        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM shop_items WHERE shop_type='episode' AND episode=?", (prev_ep,))
        prev_ep_items = [row[0] for row in cursor.fetchall()]

        cursor.execute("INSERT OR IGNORE INTO story_progress (user_id, episode) VALUES (?, ?)", (user_id, self.current_ep))

        for item in prev_ep_items:
            cursor.execute("DELETE FROM inventory WHERE user_id = ? AND item_name = ?", (user_id, item))
        conn.commit()
        conn.close()

        self.update_components()
        story_text = EPISODE_STORIES.get(self.current_ep, "« Une histoire mystérieuse... »")
        
        embed = discord.Embed(
            title=f"📜 Récit de {get_episode_title(self.current_ep)}",
            description=f"{story_text}\n\n*✨ (Guillaume a pris vos reliques de l'épisode {prev_ep} et a sauvegardé cet épisode à vie dans vos archives !) *",
            color=discord.Color.gold()
        )
        embed.set_thumbnail(url="https://images.emojiterra.com/google/android-10/512px/1f3ad.png")
        
        await interaction.response.edit_message(embed=self.build_embed(), view=self)
        await interaction.followup.send(embed=embed, ephemeral=True)

    def build_embed(self) -> discord.Embed:
        has_story = self.current_ep in EPISODE_STORIES and bool(EPISODE_STORIES[self.current_ep].strip())

        if not has_story:
            status_txt = "💤 **[Le troubadour se repose, revenez plus tard]**"
        elif self.current_ep == 1:
            status_txt = "📖 **[Épisode Gratuit & Accessible]**"
        else:
            user_id = self.member.id
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute("SELECT 1 FROM story_progress WHERE user_id = ? AND episode = ?", (user_id, self.current_ep))
            is_unlocked = cursor.fetchone() is not None
            conn.close()

            status_txt = "📖 **[Débloqué & Sauvegardé]**" if is_unlocked else f"🔒 **[Verrouillé / Reliques de l'épisode {self.current_ep - 1} requises]**"

        embed = discord.Embed(
            title=f"🪕 Guillaume le Troubadour — {get_episode_title(self.current_ep)}",
            description=(
                f"Statut : {status_txt}\n\n"
                "Utilisez les boutons ci-dessous pour naviguer entre les épisodes, donner vos reliques ou écouter les récits de votre choix !"
            ),
            color=discord.Color.purple()
        )
        embed.set_thumbnail(url="https://images.emojiterra.com/google/android-10/512px/1f3ad.png")
        return embed

class PersistentTroubadourView(ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @ui.button(label="Parler à Guillaume", style=discord.ButtonStyle.success, emoji="🪕", custom_id="persistent_troubadour_talk_main")
    async def talk_to_troubadour(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.defer(ephemeral=True)

        if not isinstance(interaction.user, discord.Member):
            return await interaction.followup.send("❌ Erreur.", ephemeral=True)

        view = TroubadourPaginationView(interaction.user, current_ep=1)
        await interaction.followup.send(embed=view.build_embed(), view=view, ephemeral=True)

# --- VUE POUR LA BOUTIQUE DES ÉPISODES ---
class EpisodeShopView(ui.View):
    def __init__(self, member: discord.Member, episode_num: int):
        super().__init__(timeout=120)
        self.member = member
        self.episode_num = episode_num
        self.load_items()

    def load_items(self):
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT item_key, name, price FROM shop_items WHERE shop_type = 'episode' AND episode = ?", (self.episode_num,))
        items = cursor.fetchall()

        all_bought = True
        for item_key, name, price in items:
            cursor.execute("SELECT quantity FROM inventory WHERE user_id=? AND item_name=?", (self.member.id, name))
            res = cursor.fetchone()
            has_item = bool(res and res[0] > 0)

            owned = has_item

            if not owned:
                all_bought = False

            button = ui.Button(
                label=f"Possédé : {name}" if owned else f"Acheter {name} ({format_currency(price)})",
                style=discord.ButtonStyle.secondary if owned else discord.ButtonStyle.success,
                custom_id=f"ep_buy_{item_key}_{self.episode_num}",
                disabled=owned,
                row=0
            )
            button.callback = self.create_callback(item_key, name, price)
            self.add_item(button)

        if all_bought and len(items) > 0 and self.episode_num < 25:
            next_btn = ui.Button(
                label="➡️ Épisode Suivant", 
                style=discord.ButtonStyle.primary, 
                custom_id=f"next_ep_{self.episode_num}",
                row=1
            )
            next_btn.callback = self.next_episode_callback
            self.add_item(next_btn)
        conn.close()

    def create_callback(self, item_key: str, item_name: str, item_price: int):
        async def callback(interaction: discord.Interaction):
            if interaction.user.id != self.member.id:
                return await interaction.response.send_message("❌ Ce n'est pas votre boutique !", ephemeral=True)
            
            wallet, _, _, _ = get_user(self.member.id)
            if wallet < item_price:
                return await interaction.response.send_message(f"❌ Solde insuffisant ! Il te manque {format_currency(item_price - wallet)}.", ephemeral=True)

            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute("UPDATE users SET wallet = wallet - ? WHERE user_id = ?", (item_price, self.member.id))
            cursor.execute("""
                INSERT INTO inventory (user_id, item_name, quantity) VALUES (?, ?, 1)
                ON CONFLICT(user_id, item_name) DO UPDATE SET quantity = quantity + 1
            """, (self.member.id, item_name))
            conn.commit()
            conn.close()

            new_view = EpisodeShopView(self.member, self.episode_num)
            
            title = get_episode_title(self.episode_num)
            embed = discord.Embed(title=f"📜 {title}", color=discord.Color.dark_teal())
            embed.description = "Achète tous les objets de cet épisode !"
            
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute("SELECT name, price, description FROM shop_items WHERE shop_type='episode' AND episode=?", (self.episode_num,))
            items = cursor.fetchall()
            conn.close()
                
            for n, p, desc in items:
                embed.add_field(name=n, value=f"Prix : **{format_currency(p)}**\n*{desc}*", inline=False)
            
            await interaction.response.edit_message(embed=embed, view=new_view)
            await interaction.followup.send(f"✅ Tu as acheté **{item_name}** pour {format_currency(item_price)} !", ephemeral=True)
        return callback

    async def next_episode_callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.member.id:
            return await interaction.response.send_message("❌ Ce n'est pas votre boutique !", ephemeral=True)
        
        next_ep = self.episode_num + 1
        new_view = EpisodeShopView(self.member, next_ep)
        
        title = get_episode_title(next_ep)
        embed = discord.Embed(title=f"📜 {title}", color=discord.Color.dark_teal())
        embed.description = "Achète tous les objets de cet épisode !"
        
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT name, price, description FROM shop_items WHERE shop_type='episode' AND episode=?", (next_ep,))
        items = cursor.fetchall()
        conn.close()
            
        for n, p, desc in items:
            embed.add_field(name=n, value=f"Prix : **{format_currency(p)}**\n*{desc}*", inline=False)
        
        await interaction.response.edit_message(embed=embed, view=new_view)

# --- VUE POUR LA BOUTIQUE DYNAMIQUE CLASSIQUE ---
class DynamicShopView(ui.View):
    def __init__(self, member: discord.Member, shop_type: str):
        super().__init__(timeout=60)
        self.member = member
        self.shop_type = shop_type
        self.load_items()

    def load_items(self):
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT item_key, name, price, required_role_id FROM shop_items WHERE shop_type = ?", (self.shop_type,))
        items = cursor.fetchall()
        conn.close()

        for item_key, name, price, required_role_id in items:
            if required_role_id is not None:
                role = self.member.guild.get_role(required_role_id)
                if not role or role not in self.member.roles:
                    continue

            button = ui.Button(
                label=f"Acheter {name} ({format_currency(price)})",
                style=discord.ButtonStyle.success,
                custom_id=f"shop_buy_{item_key}"
            )
            button.callback = self.create_callback(item_key)
            self.add_item(button)

    def create_callback(self, item_key: str):
        async def callback(interaction: discord.Interaction):
            if interaction.user.id != self.member.id:
                return await interaction.response.send_message("❌ Ce n'est pas votre boutique !", ephemeral=True)
            
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute("SELECT name, price, role_to_give_id FROM shop_items WHERE item_key = ?", (item_key,))
            item = cursor.fetchone()
            conn.close()

            if not item:
                return await interaction.response.send_message("❌ Cet article n'existe plus.", ephemeral=True)

            item_name, item_price, role_to_give_id = item
            wallet, _, _, _ = get_user(self.member.id)

            if wallet < item_price:
                return await interaction.response.send_message(f"❌ Solde insuffisant ! Il te manque {format_currency(item_price - wallet)}.", ephemeral=True)

            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute("UPDATE users SET wallet = wallet - ? WHERE user_id = ?", (item_price, self.member.id))
            cursor.execute("""
                INSERT INTO inventory (user_id, item_name, quantity) VALUES (?, ?, 1)
                ON CONFLICT(user_id, item_name) DO UPDATE SET quantity = quantity + 1
            """, (self.member.id, item_name))
            conn.commit()
            conn.close()

            feedback_extra = ""
            if role_to_give_id:
                role_to_give = interaction.guild.get_role(role_to_give_id)
                if role_to_give:
                    try:
                        await self.member.add_roles(role_to_give)
                        feedback_extra = f" et le rôle **{role_to_give.name}** t'a été attribué !"
                    except discord.Forbidden:
                        feedback_extra = "\n⚠️ *Achat réussi, mais le bot manque de permissions pour attribuer le rôle.*"

            await interaction.response.send_message(f"✅ Achat réussi ! Tu as acheté **{item_name}** pour {format_currency(item_price)}{feedback_extra}", ephemeral=True)
        return callback

# --- MENU DE DIALOGUE PRINCIPAL DU MARCHAND ---
class ShopDialogueView(ui.View):
    def __init__(self, member: discord.Member):
        super().__init__(timeout=120)
        self.member = member
        
        has_special_access = False
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT required_role_id FROM shop_items WHERE shop_type = 'special'")
        rows = cursor.fetchall()
        conn.close()

        for row in rows:
            r_id = row[0]
            if r_id:
                r = self.member.guild.get_role(r_id)
                if r and r in self.member.roles:
                    has_special_access = True
                    break

        if has_special_access:
            special_button = ui.Button(
                label="✨ Boutique Spéciale (Inédits)",
                style=discord.ButtonStyle.blurple,
                custom_id="shop_dialogue_special"
            )
            special_button.callback = self.open_special_shop
            self.add_item(special_button)

    @ui.button(label="🛒 Voir la boutique", style=discord.ButtonStyle.primary, emoji="✨", custom_id="shop_dialogue_browse")
    async def open_shop(self, interaction: discord.Interaction, button: ui.Button):
        if interaction.user.id != self.member.id:
            return await interaction.response.send_message("❌ Ce n'est pas ton tour de parler au marchand !", ephemeral=True)
        
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT name, price, description, required_role_id FROM shop_items WHERE shop_type = 'normal'")
        items = cursor.fetchall()
        conn.close()

        embed = discord.Embed(title="🛒 Boutique Normale", color=discord.Color.gold())
        embed.description = "Voici les objets disponibles :"

        for name, price, desc, required_role_id in items:
            embed.add_field(name=name, value=f"Prix : **{format_currency(price)}**\n*{desc}*", inline=False)

        view = DynamicShopView(interaction.user, 'normal')
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    @ui.button(label="📖 Boutique Histoire", style=discord.ButtonStyle.success, emoji="📜", custom_id="shop_dialogue_story")
    async def open_story_shop(self, interaction: discord.Interaction, button: ui.Button):
        if interaction.user.id != self.member.id:
            return await interaction.response.send_message("❌ Ce n'est pas ton tour !", ephemeral=True)
            
        current_ep = 1
        view = EpisodeShopView(interaction.user, current_ep)
        title = get_episode_title(current_ep)
        embed = discord.Embed(title=f"📜 {title}", color=discord.Color.dark_teal())
        embed.description = "Achète tous les objets de cet épisode !"
        
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT name, price, description FROM shop_items WHERE shop_type='episode' AND episode=?", (current_ep,))
        items = cursor.fetchall()
        conn.close()
            
        for name, price, desc in items:
            embed.add_field(name=name, value=f"Prix : **{format_currency(price)}**\n*{desc}*", inline=False)

        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    async def open_special_shop(self, interaction: discord.Interaction):
        if interaction.user.id != self.member.id:
            return await interaction.response.send_message("❌ Ce n'est pas ton tour !", ephemeral=True)

        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT name, price, description FROM shop_items WHERE shop_type = 'special'")
        items = cursor.fetchall()
        conn.close()

        embed = discord.Embed(title="✨ Boutique Spéciale & Inédite", color=discord.Color.purple())
        embed.description = "Félicitations pour ton accès exclusif ! Voici les articles inédits :"

        for name, price, desc in items:
            embed.add_field(name=name, value=f"Prix : **{format_currency(price)}**\n*{desc}*", inline=False)

        view = DynamicShopView(interaction.user, 'special')
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    @ui.button(label="🎒 Voir mon inventaire", style=discord.ButtonStyle.secondary, emoji="📦", custom_id="shop_dialogue_inventory")
    async def open_inventory(self, interaction: discord.Interaction, button: ui.Button):
        if interaction.user.id != self.member.id:
            return await interaction.response.send_message("❌ Ce n'est pas ton tour de parler au marchand !", ephemeral=True)
        
        user_id = interaction.user.id
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT item_name, quantity FROM inventory WHERE user_id = ?", (user_id,))
        rows = cursor.fetchall()
        conn.close()

        embed = discord.Embed(title=f"🎒 Inventaire de {interaction.user.display_name}", color=discord.Color.blue())
        if not rows:
            embed.description = "Ton inventaire est désespérément vide..."
        else:
            description = [f"• **{item_name}** x`{qty}`" for item_name, qty in rows]
            embed.description = "\n".join(description)

        await interaction.response.send_message(embed=embed, ephemeral=True)

    @ui.button(label="👋 Au revoir !", style=discord.ButtonStyle.danger, emoji="🚪", custom_id="shop_dialogue_leave")
    async def leave_chat(self, interaction: discord.Interaction, button: ui.Button):
        if interaction.user.id != self.member.id:
            return await interaction.response.send_message("❌ Ce n'est pas ton tour !", ephemeral=True)
        await interaction.response.defer()
        await interaction.delete_original_response()

class PersistentMerchantView(ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @ui.button(label="Parler au Marchand", style=discord.ButtonStyle.success, emoji="🦊", custom_id="persistent_merchant_talk_main")
    async def talk_to_merchant(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.defer(ephemeral=True)

        if not isinstance(interaction.user, discord.Member):
            return await interaction.followup.send("❌ Erreur.", ephemeral=True)

        embed = discord.Embed(
            title="🦊 Tom - Le Marchand Ambulant",
            description=(
                f"Oh, bonjour **{interaction.user.display_name}** ! "
                "Bienvenue dans ma boutique exclusive !\n\n"
                "*Qu'est-ce qui t'amène par ici aujourd'hui ? Fais ton choix camarade...*"
            ),
            color=discord.Color.orange()
        )
        embed.set_thumbnail(url="https://images.emojiterra.com/google/android-10/512px/1f98a.png")

        view = ShopDialogueView(interaction.user)
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)

@bot.tree.command(name="balance", description="Vérifie ton solde ou celui d'un autre utilisateur")
async def balance(interaction: discord.Interaction, member: discord.Member = None):
    target = member or interaction.user
    wallet, bank, _, _ = get_user(target.id)
    embed = discord.Embed(title=f"Portefeuille de {target.display_name}", color=discord.Color.blurple())
    embed.add_field(name="Portefeuille", value=format_currency(wallet), inline=True)
    embed.add_field(name="Banque", value=format_currency(bank), inline=True)
    embed.set_thumbnail(url=target.display_avatar.url)
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="setup-marchand", description="[Admin] Installe le PNJ permanent dans le salon actuel")
@commands.has_permissions(administrator=True)
async def setup_marchand(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    embed = discord.Embed(
        title="✨ Bienvenue au Salon du Shop !",
        description=(
            "🦊 **Tom le Marchand** est installé ici en permanence.\n\n"
            "👉 **Clique sur le bouton ci-dessous** pour engager la discussion avec lui !"
        ),
        color=discord.Color.gold()
    )
    embed.set_thumbnail(url="https://images.emojiterra.com/google/android-10/512px/1f98a.png")
    view = PersistentMerchantView()
    await interaction.channel.send(embed=embed, view=view)
    await interaction.followup.send("✅ Le PNJ marchand a été installé avec succès dans ce salon !", ephemeral=True)

@setup_marchand.error
async def setup_marchand_error(interaction: discord.Interaction, error):
    if isinstance(error, discord.app_commands.MissingPermissions):
        if not interaction.response.is_done():
            await interaction.response.send_message("❌ Permission refusée.", ephemeral=True)
        else:
            await interaction.followup.send("❌ Permission refusée.", ephemeral=True)
    else:
        if not interaction.response.is_done():
            await interaction.response.send_message(f"❌ Erreur : {error}", ephemeral=True)
        else:
            await interaction.followup.send(f"❌ Erreur : {error}", ephemeral=True)

@bot.tree.command(name="setup-troubadour", description="[Admin] Installe Guillaume le Troubadour permanent dans le salon actuel")
@commands.has_permissions(administrator=True)
async def setup_troubadour(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    embed = discord.Embed(
        title="🪕 Guillaume le Troubadour",
        description=(
            "✨ **Guillaume** est arrivé pour conter les épopées de vos voyages.\n\n"
            "👉 **Clique sur le bouton ci-dessous** pour lui parler et lui donner vos reliques d'épisodes !"
        ),
        color=discord.Color.purple()
    )
    embed.set_thumbnail(url="https://images.emojiterra.com/google/android-10/512px/1f3ad.png")
    view = PersistentTroubadourView()
    await interaction.channel.send(embed=embed, view=view)
    await interaction.followup.send("✅ Guillaume le Troubadour a été installé avec succès dans ce salon !", ephemeral=True)

@setup_troubadour.error
async def setup_troubadour_error(interaction: discord.Interaction, error):
    if isinstance(error, discord.app_commands.MissingPermissions):
        if not interaction.response.is_done():
            await interaction.response.send_message("❌ Permission refusée.", ephemeral=True)
        else:
            await interaction.followup.send("❌ Permission refusée.", ephemeral=True)
    else:
        if not interaction.response.is_done():
            await interaction.response.send_message(f"❌ Erreur : {error}", ephemeral=True)
        else:
            await interaction.followup.send(f"❌ Erreur : {error}", ephemeral=True)

@bot.tree.command(name="reset-story", description="[Admin] Réinitialise la progression des histoires et supprime les reliques des inventaires")
@commands.has_permissions(administrator=True)
async def reset_story(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM story_progress")
    cursor.execute("DELETE FROM inventory WHERE item_name LIKE '%Relique%'")
    conn.commit()
    conn.close()

    await interaction.followup.send("🔄 **Réinitialisation réussie !** Toutes les histoires validées et les reliques d'épisodes ont été remises à zéro pour les tests.", ephemeral=True)

@reset_story.error
async def reset_story_error(interaction: discord.Interaction, error):
    if isinstance(error, discord.app_commands.MissingPermissions):
        if not interaction.response.is_done():
            await interaction.response.send_message("❌ Permission refusée.", ephemeral=True)
        else:
            await interaction.followup.send("❌ Permission refusée.", ephemeral=True)
    else:
        if not interaction.response.is_done():
            await interaction.response.send_message(f"❌ Erreur : {error}", ephemeral=True)
        else:
            await interaction.followup.send(f"❌ Erreur : {error}", ephemeral=True)

@bot.tree.command(name="inventory", description="Affiche ton inventaire d'achats")
async def inventory(interaction: discord.Interaction):
    user_id = interaction.user.id
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT item_name, quantity FROM inventory WHERE user_id = ?", (user_id,))
    rows = cursor.fetchall()
    conn.close()

    embed = discord.Embed(title=f"🎒 Inventaire de {interaction.user.display_name}", color=discord.Color.blue())
    if not rows:
        embed.description = "Ton inventaire est désespérément vide..."
    else:
        description = [f"• **{item_name}** x`{qty}`" for item_name, qty in rows]
        embed.description = "\n".join(description)
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="shop_add", description="[Admin] Ajoute un article normal, spécial ou épisode")
@commands.has_permissions(administrator=True)
async def shop_add(
    interaction: discord.Interaction, 
    item_key: str, 
    name: str, 
    price: int, 
    description: str, 
    shop_type: str = "normal", 
    episode: int = 0,
    required_role: discord.Role = None, 
    role_to_give: discord.Role = None
):
    await interaction.response.defer(ephemeral=True)
    req_role_id = required_role.id if required_role else None
    give_role_id = role_to_give.id if role_to_give else None

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO shop_items (item_key, name, price, description, shop_type, episode, required_role_id, role_to_give_id) 
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(item_key) DO UPDATE SET 
            name = ?, price = ?, description = ?, shop_type = ?, episode = ?, required_role_id = ?, role_to_give_id = ?
    """, (item_key, name, price, description, shop_type, episode, req_role_id, give_role_id, 
          name, price, description, shop_type, episode, req_role_id, give_role_id))
    conn.commit()
    conn.close()

    ep_txt = f" (Épisode {episode})" if shop_type == "episode" else ""
    await interaction.followup.send(f"✅ L'article **{name}** a été ajouté au shop **{shop_type}**{ep_txt} avec succès !", ephemeral=True)

@shop_add.error
async def shop_add_error(interaction: discord.Interaction, error):
    if isinstance(error, discord.app_commands.MissingPermissions):
        if not interaction.response.is_done():
            await interaction.response.send_message("❌ Permission refusée.", ephemeral=True)
        else:
            await interaction.followup.send("❌ Permission refusée.", ephemeral=True)
    else:
        if not interaction.response.is_done():
            await interaction.response.send_message(f"❌ Erreur : {error}", ephemeral=True)
        else:
            await interaction.followup.send(f"❌ Erreur : {error}", ephemeral=True)

@bot.tree.command(name="shop_remove", description="[Admin] Supprime un article de la boutique")
@commands.has_permissions(administrator=True)
async def shop_remove(interaction: discord.Interaction, item_key: str):
    await interaction.response.defer(ephemeral=True)
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM shop_items WHERE item_key = ?", (item_key,))
    deleted = cursor.rowcount
    conn.commit()
    conn.close()

    if deleted > 0:
        await interaction.followup.send(f"✅ Article `{item_key}` supprimé.", ephemeral=True)
    else:
        await interaction.followup.send(f"❌ Aucun article trouvé avec la clé `{item_key}`.", ephemeral=True)

@shop_remove.error
async def shop_remove_error(interaction: discord.Interaction, error):
    if isinstance(error, discord.app_commands.MissingPermissions):
        if not interaction.response.is_done():
            await interaction.response.send_message("❌ Permission refusée.", ephemeral=True)
        else:
            await interaction.followup.send("❌ Permission refusée.", ephemeral=True)
    else:
        if not interaction.response.is_done():
            await interaction.response.send_message(f"❌ Erreur : {error}", ephemeral=True)
        else:
            await interaction.followup.send(f"❌ Erreur : {error}", ephemeral=True)

TOKEN = os.getenv("SHOP_TOKEN")
