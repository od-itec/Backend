pipeline {
  agent any
  
  stages {
	  stage('Push Image to Docker Registry') {
		  steps {
				script {
					withDockerRegistry(url: 'https://registry.onlinedi.vision:5000',  credentialsId:'docker-registry') {
						sh "docker build . -t itec-backend:v0.0.1"
						sh "docker tag itec-backend:v0.0.1 registry.onlinedi.vision:5000/itec-backend:v0.0.1"
						sh "docker push registry.onlinedi.vision:5000/itec-backend:v0.0.1"
					}
				}
			}
		}
  }
}

