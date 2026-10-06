mkdir -p sparql-endpoint && curl -L --fail \
  --output sparql-endpoint/apache-jena-fuseki-6.2.0.tar.gz \
  https://dlcdn.apache.org/jena/binaries/apache-jena-fuseki-6.2.0.tar.gz

cd sparql-endpoint/
tar -xzf apache-jena-fuseki-6.2.0.tar.gz
cd apache-jena-fuseki-6.2.0/
chmod +x fuseki-server
