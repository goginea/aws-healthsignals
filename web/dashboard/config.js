/* Dashboard runtime config.
 *
 * This committed file holds empty defaults for local development. At deploy
 * time the CDK BucketDeployment OVERWRITES this file with the real API URL,
 * AWS region, and Cognito app-client id for the deployed environment, so the
 * dashboard is fully configured with no manual step.
 */
window.DASHBOARD_CONFIG = {
  apiUrl: "",
  region: "",
  userPoolClientId: ""
};
