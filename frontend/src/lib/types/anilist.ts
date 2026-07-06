export type MediaType = "ANIME" | "MANGA";

export type MediaFormat =
    | "TV"
    | "TV_SHORT"
    | "MOVIE"
    | "SPECIAL"
    | "OVA"
    | "ONA"
    | "MUSIC"
    | "MANGA"
    | "NOVEL"
    | "ONE_SHOT";

export type MediaStatus =
    "FINISHED" | "RELEASING" | "NOT_YET_RELEASED" | "CANCELLED" | "HIATUS";

export type MediaSeason = "WINTER" | "SPRING" | "SUMMER" | "FALL";

export interface MediaTitle {
    romaji?: string | null;
    english?: string | null;
    native?: string | null;
    userPreferred?: string | null;
}

export interface MediaCoverImage {
    // extraLarge?: string | null;
    // large?: string | null;
    medium?: string | null;
    // color?: string | null;
}

export interface Media {
    id: number;
    // id_mal?: number | null;
    // type?: MediaType | null;
    format?: MediaFormat | null;
    status?: MediaStatus | null;
    season?: MediaSeason | null;
    seasonYear?: number | null;
    episodes?: number | null;
    duration?: number | null;
    coverImage?: MediaCoverImage | null;
    // bannerImage?: string | null;
    // synonyms?: string[] | null;
    // isLocked?: boolean | null;
    isAdult?: boolean | null;
    title?: MediaTitle | null;
    // startDate?: FuzzyDate | null;
    // endDate?: FuzzyDate | null;
    // nextAiringEpisode?: AiringSchedule | null;
}
