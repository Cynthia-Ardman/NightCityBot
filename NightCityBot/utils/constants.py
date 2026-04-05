# Role costs and related mappings
ROLE_COSTS_BUSINESS = {
    "Business Tier 0": 0,
    "Business Tier 1": 2000,
    "Business Tier 2": 3000,
    "Business Tier 3": 5000
}

ROLE_COSTS_HOUSING = {
    "Housing Tier 1": 1000,
    "Housing Tier 2": 2000,
    "Housing Tier 3": 3000
}

TRAUMA_ROLE_COSTS = {
    "Trauma Team Silver": 1000,
    "Trauma Team Gold": 2000,
    "Trauma Team Plat": 4000,
    "Trauma Team Diamond": 10000
}

# Constants for economy
BASELINE_LIVING_COST = 500

# Passive income scales
TIER_0_INCOME_SCALE = {1: 150, 2: 250, 3: 350, 4: 500}
OPEN_PERCENT = {0: 0, 1: 0.25, 2: 0.4, 3: 0.6, 4: 0.8}
ATTEND_REWARD = 250

# Commands from UnbelievaBoat to ignore in unknown command handler
UNBELIEVABOAT_COMMANDS = {
    "add-cash-role", "add-fail-reply", "add-failed-reply", "add-fine-reply",
    "add-money", "addmoney", "add-money-role", "add-payout-reply", "add-reply", "agree",
    "agree-channel", "agree-role", "aki", "akinator", "amped-fm", "amped.fm", "animal",
    "animal-race", "animal-scale", "animals", "animals-scale", "ass",
    "assignable-role", "assignable-roles", "auto-mod-ignore", "auto-mod-ignored",
    "auto-mod-invites", "auto-mod-mentions", "auto-mod-silenced", "auto-mod-silent",
    "auto-moderation-invites", "auto-moderation-mentions", "auto-play", "auto-role",
    "auto-roles", "b", "b-boost", "bal", "balance", "ban", "banne", "barn", "bass",
    "bass-boost", "bass-boosted", "bean", "bet-limit", "bet-limits", "bj", "bj-decks",
    "blackjack", "blackjack-decks", "boob", "boobies", "boobs", "booty", "bot-commander",
    "bot-commander-role", "butt", "butts", "buy", "buy-item", "c-f", "c-mute", "c-un-mute",
    "case", "case-edit", "case-reason", "cat", "cat-pic", "cats", "cf-win-chance",
    "cf-win-rate", "change-currency", "channel", "channel-agree", "channel-info",
    "channel-lock-down", "channel-override", "channel-overwrite", "channel-permit",
    "chat-amount", "chat-channel", "chat-channels", "chat-cooldown", "chat-money",
    "chat-money-amount", "chat-money-channel", "chat-money-channels", "chat-money-cooldown",
    "chat-mute", "chat-un-mute", "check-upvoted", "chicken-fight", "choose",
    "clean-lb", "clean-leaderboard", "clear", "clear-queue", "cmc", "cock-fight",
    "cock-fight-win-chance", "cock-fight-win-rate", "collect", "collect-income",
    "collect-role-income", "command-cooldown", "command-status", "commander-role",
    "commands", "config-currency", "cooldown", "cooldown-set", "counting",
    "counting-channel", "create-item", "crime", "currency-set", "currency-symbol",
    "custom-replies", "dad-joke", "daddy-joke", "dashboard", "dc", "dec-cash-role",
    "dec-money", "decrease-cash-role", "default-replies", "default-reply", "delete",
    "delete-case", "delete-item", "delete-log", "delete-reminder", "delete-replies",
    "delete-reply", "delete-warning", "deleted-log", "deleted-message-log", "dep",
    "deposit", "dice", "dick", "dictate", "disable", "disconnect", "discord-fm",
    "discord.fm", "docs", "dog", "dog-pic", "dogging", "doggo", "doggy", "dogs",
    "donate", "donate-money", "donation-verify", "economy-audit-log", "economy-reset",
    "economy-stats", "edit-case", "edit-item", "edit-log", "edit-reason", "edited-log",
    "edited-message-log", "enable", "escort", "factory-reset", "fail-rate",
    "fail-rate-set", "fail-reply-add", "feedback", "fight-chicken", "fight-cock",
    "fine-amount", "fine-rate-set", "fine-reply-add", "fine-set", "fine-set-amount",
    "fine-type", "fine-type-set", "force-ban", "forget", "forget-reminder",
    "game-cooldown", "game-cooldown-set", "give", "give-away", "give-item",
    "give-money", "goodbye-channel", "goodbye-message", "hack-ban", "hard-kick",
    "help", "history", "i-am", "i-am-not", "ignore-auto-mod", "inc-cash-role",
    "inc-money", "increase-cash-role", "inv", "inventory", "invite",
    "invite-moderation", "invite-protection", "invite-whitelist", "invites",
    "invites-whitelist", "item-buy", "item-create", "item-edit", "item-give",
    "item-info", "item-options", "item-update", "item-use", "join", "join-channel",
    "join-message", "join-money", "join-race", "join-role", "joke", "k", "kick",
    "kitten", "kitty", "lb", "lb-clean", "leader-board-clean", "leaderboard",
    "leave", "leave-channel", "leave-message", "leave-role", "left-channel",
    "list-assignable-roles", "list-auto-roles", "list-custom-replies", "list-reminders",
    "list-replies", "list-self-roles", "lock-down", "lock-down-channel",
    "log-channel", "loop", "loop-queue", "m", "mass-role", "max-bal", "max-balance",
    "maximum-balance", "member", "member-agree", "member-info", "mention-moderation",
    "mention-spam-protection", "mentions", "message-delete-log", "message-deleted-log",
    "message-edit-log", "message-edited-log", "message-log-ignore", "message-log-ignored",
    "mod-channel", "mod-commands", "mod-log", "mod-log-channel", "mod-role",
    "moderation-log", "moderator-role", "module-status", "money", "money-add",
    "money-add-role", "money-audit-log", "money-give", "money-remove", "money-reset",
    "money-rm-role", "mute", "mute-role", "muted", "muted-role", "never-ever",
    "never-have-i", "never-have-i-ever", "new-item", "next-track", "nhie", "now-playing",
    "np", "override", "override-channel", "overwrite-channel", "patreon", "pause",
    "pay", "payout-set", "paypal", "penis", "perm", "permission", "permissions",
    "permit", "perms", "pick", "play", "play-list", "playlist", "playlists", "prefix",
    "premium", "profile", "provision", "provisions", "prune", "punishment",
    "punishments", "purge", "pussy", "qoute", "queue", "quote", "quote-message",
    "r34", "radio", "radio-station", "random", "random-react", "random-reaction",
    "random-user", "random-user-reaction", "reason", "reason-edit", "reason-set",
    "reddit", "remind", "remind-me", "reminder-delete", "reminder-forget",
    "reminder-list", "reminders", "remove-ban", "remove-case", "remove-cash-role",
    "remove-item", "remove-money", "removemoney", "remove-money-role", "remove-punishment",
    "remove-replies", "remove-reply", "remove-warning", "repeat", "repeat-queue",
    "reply-add", "request", "requests", "reset-balance", "reset-economy",
    "reset-money", "resume", "rm-ban", "rm-case", "rm-cash-role", "rm-item",
    "rm-money", "rm-money-role", "rm-punish", "rm-punishment", "rm-replies",
    "rm-reply", "rob", "role", "role-add-money", "role-agree", "role-id",
    "role-income", "role-info", "role-me", "roll", "roll-dice", "roulete",
    "roulette", "roulette-info", "roullete", "roullette", "rr", "rtfm", "rule-34",
    "rus-roulette", "russian-roulete", "russian-roulette", "russian-roullete",
    "russian-roullette", "s-b", "s-m", "say", "scale-animal", "scale-animals",
    "search", "seek", "self-role", "self-roles", "sell", "sell-item", "server",
    "server-feedback", "server-info", "server-stats", "server-suggest",
    "set-bet", "set-bet-limit", "set-bj-decks", "set-blackjack-decks", "set-cooldown",
    "set-currency", "set-currency-symbol", "set-fail-rate", "set-fine",
    "set-fine-amount", "set-fine-rate", "set-fine-type", "set-game-cooldown",
    "set-join-balance", "set-join-money", "set-max-balance", "set-payout",
    "set-payout-amount", "set-prefix", "set-reason", "set-reminder",
    "set-start-balance", "set-start-money", "set-starting-balance", "set-volume",
    "shop", "show-reminders", "shuffle", "shuffle-queue", "silent-auto-mod",
    "skip", "skip-song", "slot", "slot-emote", "slot-machine", "slot-machine-emote",
    "slot-machine-symbol", "slot-machine-symbols", "slot-symbol", "slots",
    "slow-mode", "slut", "soft-ban", "start-balance", "start-money",
    "starting-balance", "starting-money", "station", "stations", "stop", "store",
    "stream", "sub-reddit", "suggest", "suggestion", "suggestion-channel",
    "suggestions", "suggestions-channel", "support-server", "t-b", "t-mute",
    "t-un-mute", "take-item", "temp-ban", "temporary-ban", "test", "text-mute",
    "text-un-mute", "tits", "titties", "tod", "top", "trade-item", "truth-dare",
    "truth-or-dare", "un-ban", "un-mute", "un-pause", "update-item", "upvote",
    "upvoted", "use", "use-item", "user", "user-info", "user-join", "user-leave",
    "user-left", "user-left-channel", "v-d", "v-k", "verify-donation",
    "verify-pay-pal", "view-case", "voice-disconnect", "voice-kick", "volume",
    "volume-level", "vote", "vote-skip", "w", "warn", "warnings", "welcome-channel",
    "welcome-message", "whitelisted-invite", "whitelisted-invites", "who-is",
    "whore", "widthdraw", "widthdrawl", "with", "withdraw", "withdrawal",
    "withdrawl", "work",
}

VALID_GUN_CLASSES = {
    "pistol", "revolver", "submachine_gun", "shotgun", "assault_rifle",
    "light_machine_gun", "heavy_machine_gun", "precision_rifle", "sniper_rifle",
}

GUN_CLASS_DISPLAY_NAMES = {
    "pistol": "Pistols",
    "revolver": "Revolvers",
    "submachine_gun": "Submachine Guns",
    "shotgun": "Shotguns",
    "assault_rifle": "Assault Rifles",
    "light_machine_gun": "Light Machine Guns",
    "heavy_machine_gun": "Heavy Machine Guns",
    "precision_rifle": "Precision Rifles",
    "sniper_rifle": "Sniper Rifles",
}

GUN_CLASS_ORDER = [
    "pistol", "revolver", "submachine_gun", "shotgun", "assault_rifle",
    "light_machine_gun", "heavy_machine_gun", "precision_rifle", "sniper_rifle",
]

POWER_LEVEL_WORDS = {"L": "Low", "M": "Medium", "H": "High"}

