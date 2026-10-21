// Which build this is. Vite replaces import.meta.env.MODE with a string literal at build time, so
// `ARTIFACT_BUILD && …` branches are removed from the production bundle (see scripts/check-bundle.mjs).
// "artifact" = `vite build --mode artifact`: the private claude.ai artifact that may use the owner's connector.
export const ARTIFACT_BUILD = import.meta.env.MODE === "artifact";
