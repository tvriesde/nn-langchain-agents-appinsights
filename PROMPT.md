use tag: SecurityControl: Ignore
Region: Switzerland North

Goal of the implementation

Display the working of langchain combined with azure container apps and application insights for agent tracing.

I want you to create a simple chat interface. which lets a user request a recipe based on certain ingredients.

there is a sequential multi agent setup.

The Chef Agent: Creates the recipe
The Wine Agent: Selects the wine that fits to it
The Review agent: Reviews the work and presents it as a menu to the user

Implement the agents in

Python backend Fast API, the frontend should be react.
Use Langchain
Use an openai model deployment in azure; use openai gpt-6-luna

Deploy everything in Switserland North, if the model is not avaialable there, deploy it in Sweden CEntral

Create a deployment script that i can use to deploy the infra and the application container.


Make sure to insturment the container app with open telemetry.

make sure the agents are traced properly and i can view multi agent tracing sessions within azure application insights

put all in a single resource group


get to work.

limit the amount of testing. ill test my self.