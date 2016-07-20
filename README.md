# Overview

This charm provides the Juju Charms Review Queue, a web app for reviewing
submissions to the Juju Charm Store.

# Usage

Bare minimim deployment:

    juju deploy cs:~tvansteenburgh/review-queue
    juju deploy cs:postgresql

    # the review-queue requires postgres 9.4+
    juju set-config postgresql version='9.4' pgdg=true

    juju add-relation review-queue:db postgresql:db

To enable background tasks you must also deploy and relate a message broker:

    juju deploy rabbitmq-server
    juju add-relation review-queue rabbitmq-server

When the deployment is ready you'll see "Serving on port xxxx" in the output
of `juju status`. Browse to the ip and port listed there.

To enable all features, please see the Configuration section below.

## Scale out Usage

You can horizontally scale the review-queue by adding haproxy, with multiple
review-queue units behind it:

    juju deploy haproxy
    juju add-relation review-queue haproxy

    juju add-unit -n 2 review-queue

# Configuration

To enable Jenkins integration for testing incoming submissions, you must set
the appropriate configuration, for example:

    juju set-config review-queue \
      testing_jenkins_url=http://juju-ci.vapour.ws:8080/job/charm-bundle-test-wip/buildWithParameters \
      testing_jenkins_token=secrettoken

To enable email notifications from the app, you must provide a Sendgrid API
key with mail-send permissions, for example:

    juju set-config review-queue sendgrid_api_key=mysecretsendgridapikey

You should also set `base_url` to the url at which you are running the app to
ensure that links in generated emails point to the right place:

    juju set-config review-queue base_url=http://review.juju.solutions


## Upstream Project - Review Queue Pyramid App

- Github: https://github.com/juju-solutions/review-queue
