/**
 * Type declaration for the build-generated CSP-safe Ajv standalone validator.
 * Pairs with src/generated/activityValidator.js (produced by scripts/gen-activity-validator.mjs).
 *
 * The runtime value is a pure function (no Ajv, no new Function) matching the shape
 * of a compiled Ajv validator: callable as validate(data) -> boolean, with .errors populated
 * on failure.
 */
import type { ErrorObject } from 'ajv';

declare const validate: ((data: unknown) => boolean) & { errors?: ErrorObject[] | null };

export default validate;
export { validate };
