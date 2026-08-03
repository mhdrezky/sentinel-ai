"""Reference lists of widely-used packages, used as typo-squat baselines.

These are *not* an allowlist — presence here only means "a name this close is
probably an impersonation attempt". Keep entries lowercase; lookups normalise.

Deliberately kept small and high-traffic. A longer list raises false positives,
because obscure real packages start colliding with each other.
"""

from __future__ import annotations

from ..models import Ecosystem

NPM: frozenset[str] = frozenset(
    """
    react react-dom react-router react-router-dom next vue vue-router svelte
    @angular/core @angular/common @angular/forms @angular/router @angular/cli
    @angular/compiler @angular/platform-browser @angular/animations
    rxjs zone.js tslib typescript webpack vite rollup esbuild babel-loader
    lodash underscore ramda immutable moment dayjs date-fns
    axios node-fetch got request superagent ws socket.io
    express koa fastify hapi nest @nestjs/core
    chalk colors commander yargs minimist inquirer ora debug
    dotenv cross-env rimraf glob semver uuid nanoid
    jest mocha chai sinon vitest cypress playwright puppeteer
    eslint prettier husky lint-staged @types/node
    mongoose sequelize prisma knex pg mysql2 redis
    tailwindcss postcss autoprefixer sass less
    jquery bootstrap d3 three chart.js
    body-parser cors helmet morgan multer passport jsonwebtoken bcrypt
    """.split()
)

PYPI: frozenset[str] = frozenset(
    """
    requests urllib3 httpx aiohttp certifi charset-normalizer idna
    numpy pandas scipy matplotlib seaborn plotly
    django flask fastapi starlette uvicorn gunicorn werkzeug jinja2
    sqlalchemy alembic psycopg2 psycopg2-binary pymongo redis
    pydantic attrs click typer rich colorama tqdm
    pytest pytest-cov tox nox hypothesis mock
    setuptools wheel pip build twine poetry
    pyyaml toml tomli python-dotenv configparser
    boto3 botocore google-cloud-storage azure-storage-blob
    cryptography pyjwt passlib bcrypt paramiko
    pillow opencv-python scikit-learn tensorflow torch transformers
    beautifulsoup4 lxml selenium scrapy
    six python-dateutil pytz packaging typing-extensions
    celery kombu flower loguru structlog
    """.split()
)

NUGET: frozenset[str] = frozenset(
    """
    newtonsoft.json system.text.json serilog serilog.aspnetcore nlog log4net
    automapper mediatr fluentvalidation polly
    xunit xunit.runner.visualstudio nunit moq fluentassertions nsubstitute
    dapper entityframework microsoft.entityframeworkcore
    microsoft.extensions.logging microsoft.extensions.configuration
    microsoft.extensions.dependencyinjection microsoft.aspnetcore.authentication.jwtbearer
    restsharp refit swashbuckle.aspnetcore
    system.identitymodel.tokens.jwt bouncycastle castle.core
    """.split()
)

COMPOSER: frozenset[str] = frozenset(
    """
    monolog/monolog guzzlehttp/guzzle guzzlehttp/psr7 psr/log psr/container
    symfony/console symfony/http-foundation symfony/finder symfony/process
    symfony/yaml symfony/routing symfony/event-dispatcher
    laravel/framework laravel/tinker illuminate/support
    doctrine/orm doctrine/dbal doctrine/collections
    phpunit/phpunit mockery/mockery fakerphp/faker
    nesbot/carbon vlucas/phpdotenv ramsey/uuid league/flysystem
    twig/twig swiftmailer/swiftmailer phpmailer/phpmailer
    """.split()
)

BY_ECOSYSTEM: dict[Ecosystem, frozenset[str]] = {
    Ecosystem.NPM: NPM,
    Ecosystem.PYPI: PYPI,
    Ecosystem.NUGET: NUGET,
    Ecosystem.COMPOSER: COMPOSER,
}


def popular_for(ecosystem: Ecosystem) -> frozenset[str]:
    return BY_ECOSYSTEM.get(ecosystem, frozenset())
