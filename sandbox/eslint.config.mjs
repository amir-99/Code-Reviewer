import tseslint from "typescript-eslint";

export default [
  { ignores: ["**/node_modules/**", "**/dist/**", "**/build/**", "**/coverage/**"] },
  ...tseslint.configs.recommended,
];
