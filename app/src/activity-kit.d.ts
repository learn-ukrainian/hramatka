// Ambient declaration to allow tsc to typecheck the app without walking
// the vendored public activity-kit .ts sources (which have their own strictness).
// Runtime still resolves to the real package via Vite alias + file: dep.
declare module '@learn-ukrainian/activity-kit' {
  export const ActivityPlayer: React.ComponentType<any>;
  export type ActivityPlayerActivity = any;
  export type ActivityPlayerProps = any;
  export type ActivityCompletionEvent = any;
  export type ActivityEditOperation = any;
  export type LuActivityV1 = any;
  // Re-export commonly used for the app (loose)
  export const TrueFalse: any;
  export const Cloze: any;
  export const MatchUp: any;
  export const Quiz: any;
  export const MarkTheWords: any;
  export const FillIn: any;
  export const ErrorCorrection: any;
  export const ReadingActivity: any;
  export const EssayResponse: any;
  export type * from 'react';
}
