export function externalAuthorityUrl(
    authority: string,
    value: string,
    // eslint-disable-next-line @typescript-eslint/no-unused-vars
    _scope?: string | null,
): string | null {
    if (!value) return null;
    switch (authority) {
        case "anilist":
            return `https://anilist.co/anime/${value}`;
        case "anidb":
            return `https://anidb.net/anime/${value}`;
        case "imdb_movie":
        case "imdb_show":
            return `https://www.imdb.com/title/${value}`;
        case "tmdb_movie":
            return `https://www.themoviedb.org/movie/${value}`;
        case "tmdb_show":
            return `https://www.themoviedb.org/tv/${value}`;
        case "tvdb_movie":
            return `https://www.thetvdb.com/dereferrer/movie/${value}`;
        case "tvdb_show":
            return `https://www.thetvdb.com/dereferrer/series/${value}`;
        case "mal":
            return `https://myanimelist.net/anime/${value}`;
        default:
            return null;
    }
}
