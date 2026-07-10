# <a href="https://anibridge.eliasbenb.dev"><img src="https://anibridge.eliasbenb.dev/assets/images/logo.png" alt="Logo" width="32" style="vertical-align: middle;"/></a> AniBridge

Sync what you watch, wherever you watch.

[![Discord Shield](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fdiscord.com%2Fapi%2Finvites%2Fey8kyQU9aD%3Fwith_counts%3Dtrue&query=%24.approximate_member_count&style=for-the-badge&logo=discord&label=Discord%20Users&labelColor=%23313338&color=%235865f2&cacheSeconds=10800)](https://discord.gg/ey8kyQU9aD) [![GitHub Shield](https://img.shields.io/github/stars/anibridge/anibridge?style=for-the-badge&logo=github&label=GitHub%20Stars&labelColor=%2324292e&color=%23f0f0f0)](https://github.com/anibridge/anibridge) [![Docker Pulls](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fghcr-badge.elias.eu.org%2Fapi%2Fanibridge%2Fanibridge%2Fanibridge&query=downloadCount&style=for-the-badge&logo=docker&label=Docker%20Pulls&color=2496ed)](https://github.com/anibridge/anibridge/pkgs/container/anibridge)

> [!IMPORTANT]
> Visit the [AniBridge documentation](https://anibridge.eliasbenb.dev) for detailed setup instructions and usage information.

AniBridge is a media synchronization tool designed to keep your anime watch activity synchronized across different viewing and tracking platforms. It is powered by a [mappings database](https://github.com/anibridge/anibridge-mappings) containing over 250,000 entries tailored to anime titles.

[![Plex](https://img.shields.io/badge/Plex-F5A623?style=for-the-badge&logo=plex&logoColor=white)](https://anibridge.eliasbenb.dev/providers/library/plex)
[![Jellyfin](https://img.shields.io/badge/Jellyfin-00A4DC?style=for-the-badge&logo=jellyfin&logoColor=white)](https://anibridge.eliasbenb.dev/providers/library/jellyfin)
[![Emby](https://img.shields.io/badge/Emby-52b54b?style=for-the-badge&logo=emby&logoColor=white)](https://anibridge.eliasbenb.dev/providers/library/emby)
[![AniList](https://img.shields.io/badge/AniList-02A9FF?style=for-the-badge&logo=anilist&logoColor=white)](https://anibridge.eliasbenb.dev/providers/list/anilist)
[![MyAnimeList](https://img.shields.io/badge/MyAnimeList-2E51A2?style=for-the-badge&logo=myanimelist&logoColor=white)](https://anibridge.eliasbenb.dev/providers/list/mal)

<!-- [![Simkl](https://img.shields.io/badge/Simkl-000000?style=for-the-badge&logo=simkl&logoColor=white)](https://anibridge.eliasbenb.dev/providers/list/simkl)
[![Trakt](https://img.shields.io/badge/Trakt-1DB954?style=for-the-badge&logo=trakt&logoColor=white)](https://anibridge.eliasbenb.dev/providers/list/trakt) -->

## Key Features

- **🔄 Comprehensive Synchronization**: Synchronizes watch status, progress, ratings, reviews, and start/completion dates between your anime library and list.
- **🔗 Provider-Agnostic**: Supports multiple media library and anime list providers through a flexible plugin system (Plex, Jellyfin, Emby, AniList, MyAnimeList, Trakt).
- **🎯 Smart Content Matching**: Uses a curated mappings database with fuzzy title search fallback and support for custom mapping overrides.
- **⚡ Optimized Performance**: Intelligent batch processing, rate limiting, and caching to minimize API usage while maximizing sync speed.
- **👥 Multi-User & Multi-Profile**: Define multiple profiles to simultaneously synchronize different users, libraries, and servers with granular configuration.
- **🖥️ Web Dashboard**: Intuitive web interface with a real-time sync timeline, profile management, custom mapping editor, and log viewer.
- **🛡️ Safe & Reliable**: Built-in dry run mode for testing and automatic backups with restoration through the web UI for easy recovery.
- **🐳 Easy Deployment**: Docker-ready with easy YAML-based configuration.

[![](https://mermaid.ink/img/pako:eNp1k8Fum0AQhl9lNYcmkWywAQeD2khtcqkUXyJVkWr7sIbxsgrsot0lqWv7GaKqhx5zyQv0VuWlmkfoQkrBVctpd_9v5t9_gC0kMkWIYZ3LuySjypDLq4Ug9vmgUY2P588PXx6bNRkvTzrF6yteX_H7it8ql3ylqNrMj58fvj6RGaactmcnyxfkreDvFE8Zzl_zghGtkjdHmTGljl2XCsvWmoM5p3qFYuWkeOtSrdFolxeUoXZzyaRTCnZE3LOu3bK9gTaN_fdaKrA5aL1ntCy5YLoGvj39_HFP_jgOi99azfZGQ4bDs901NUmGmmDJtR2k3rWhepM6AAt5y_-J-Q12RY2FXhGFFrvrcwdj7FqSlBpaF1BT33HXxe5SN_R5pRQKU-ey2XN7fAj_9Q6amvcXJJfypiot2k7oP6QwmOec1Q56I5JdYwwDYJaB2KgKB1CgKmi9hW3dYwEmwwIXENtlStXNAhZib2tKKj5KWbRlSlYsg3hNc213VWkT4wWnTNEOQZGiOpeVMBB7p9OmB8Rb-ARxMHHGYRRMJ743HgVB5A1gA_HEmzinQehHUTTy_MCf7gfwuTEdOdNwHETTYBR5_sQP_XAA9nM1Us1efpVEijVnsP8FYR4WzA?type=png)](https://mermaid.live/edit#pako:eNp1k8Fum0AQhl9lNYcmkWywAQeD2khtcqkUXyJVkWr7sIbxsgrsot0lqWv7GaKqhx5zyQv0VuWlmkfoQkrBVctpd_9v5t9_gC0kMkWIYZ3LuySjypDLq4Ug9vmgUY2P588PXx6bNRkvTzrF6yteX_H7it8ql3ylqNrMj58fvj6RGaactmcnyxfkreDvFE8Zzl_zghGtkjdHmTGljl2XCsvWmoM5p3qFYuWkeOtSrdFolxeUoXZzyaRTCnZE3LOu3bK9gTaN_fdaKrA5aL1ntCy5YLoGvj39_HFP_jgOi99azfZGQ4bDs901NUmGmmDJtR2k3rWhepM6AAt5y_-J-Q12RY2FXhGFFrvrcwdj7FqSlBpaF1BT33HXxe5SN_R5pRQKU-ey2XN7fAj_9Q6amvcXJJfypiot2k7oP6QwmOec1Q56I5JdYwwDYJaB2KgKB1CgKmi9hW3dYwEmwwIXENtlStXNAhZib2tKKj5KWbRlSlYsg3hNc213VWkT4wWnTNEOQZGiOpeVMBB7p9OmB8Rb-ARxMHHGYRRMJ743HgVB5A1gA_HEmzinQehHUTTy_MCf7gfwuTEdOdNwHETTYBR5_sQP_XAA9nM1Us1efpVEijVnsP8FYR4WzA)

## Web UI Screenshot

![Web UI Screenshot](https://anibridge.eliasbenb.dev/assets/images/screenshots/timeline.png)

_View more screenshots in the [documentation](https://anibridge.eliasbenb.dev/web/screenshots)_
